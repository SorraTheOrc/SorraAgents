#!/usr/bin/env python3
"""Verify the audit context-reduction invariant (SA-0MSISKM8F004NW1U).

Implements the AC2/AC3 verification for the audit skill's initial-session
context reduction so the criteria are verifiable from in-scope code rather
than relying on recorded comments alone:

AC2 (measurable context goal)
    Every audit pi session must start with fewer than 10K input tokens of
    static context. Verified two ways:

    * ``--check-static``: deterministic measurement of the static-context
      size pi loads per session (AGENTS.md files + skills section) using
      pi's own loader code, with and without the ``--no-context-files
      --no-skills`` flags the audit runner injects. Asserts the
      with-flags size stays under the 10K-token bound.
    * ``--check-sessions``: extracts the first-call ``usage.input`` token
      count from real audit-runner pi session files on disk and asserts
      every session starts under the 10K bound, for at least ``--min-items``
      distinct work items (AC2's "random sampling of 5 re-audited items").

AC3 (verdict spot-check)
    ``--reaudit-sample`` re-audits a deterministic sample of previously
    audited work items with the current (post-change) runner and compares
    each new "Ready to close" verdict against the persisted pre-change
    verdict; with ``--controlled`` it additionally re-audits each item with
    a flag-off runner copy (the pre-change code path) so any divergence is
    attributable to the context-reduction change or not. No verdict
    regression caused by the change is a PASS.

Exit status: 0 if all performed checks pass, 1 otherwise. A structured
report is printed to stdout and (when ``--report-dir`` is given) written
as JSON + Markdown for the work item's evidence record.

This script is intentionally self-contained (stdlib only) so it runs in
CI and on operator machines without extra dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TOKEN_BOUND = 10_000
"""AC2 bound: every audit session starts below this many input tokens."""

BYTES_PER_TOKEN_ESTIMATE = 4.0
"""Documented heuristic: ~4 bytes per token for the static-context byte
measurement (English prose, markdown). Used only by --check-static, which
reports both bytes and the token estimate; --check-sessions uses the
provider-reported usage.input directly."""

# Regex matching work item ids, e.g. SA-0MSISKM8F004NW1U or OSL-0MSABC7SB001NVUN.
WORK_ITEM_RE = re.compile(r"[A-Z]{2,3}-[A-Z0-9]{16}")

# Prompt markers that identify a session as an audit-runner call. The audit
# prompt always STARTS with one of these prefixes (verified against
# audit_runner.py prompt constructors); the implement skill references the
# same marker in its own text, so containment is not enough — we require the
# first user text block to start with it.
AUDIT_PROMPT_PREFIXES = (
    "[READ-ONLY AUDIT]",
    "[PERSIST-AUDIT]",
)

TIMING_LINE_RE = re.compile(
    r"Per-call timing: issue_id=(\S+) context=(\S+) elapsed_seconds=([\d.]+)"
    r"(?: input_tokens=(\d+))?"
)
"""Parse the runner's per-call timing line (audit_runner.py ``_call_pi``)."""

READY_RE = re.compile(r"Ready to close:\s*(Yes|No)")
"""Parse the "Ready to close" verdict from audit output."""


# ---------------------------------------------------------------------------
# Static context measurement (AC2, deterministic)
# ---------------------------------------------------------------------------

LOADER_MEASUREMENT_MJS = r"""
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const PI_DIST = '/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist';

const { buildSystemPrompt } = await import(pathToFileURL(path.join(PI_DIST, 'core/system-prompt.js')).href);
const { loadProjectContextFiles } = await import(pathToFileURL(path.join(PI_DIST, 'core/resource-loader.js')).href);
const { loadSkills } = await import(pathToFileURL(path.join(PI_DIST, 'core/skills.js')).href);

const cwd = process.argv[2];
const agentDir = process.argv[3] || null;
const flags = JSON.parse(process.argv[4]); // {noContextFiles, noSkills}

function measure(opts) {
  const contextFiles = loadProjectContextFiles({ cwd, agentDir });
  const skillsResult = loadSkills({
    cwd,
    agentDir,
    skillPaths: [agentDir + '/skills', cwd + '/.pi/skills'],
    includeDefaults: false,
  });
  const skills = skillsResult.skills ?? [];
  const prompt = buildSystemPrompt({
    cwd,
    selectedTools: ['read', 'bash', 'grep', 'find', 'ls'],
    contextFiles: opts.noContextFiles ? [] : contextFiles,
    skills: opts.noSkills ? [] : skills,
  });
  return {
    bytes: Buffer.byteLength(prompt, 'utf8'),
    skills: opts.noSkills ? 0 : (skills?.length ?? 0),
  };
}

const without = measure({ noContextFiles: false, noSkills: false });
const withFlags = measure({ noContextFiles: true, noSkills: true });
console.log(JSON.stringify({ without_flags: without, with_flags: withFlags }));
"""


def measure_static_context(cwd: Path, agent_dir: Path | None) -> dict:
    """Measure static-context bytes pi would load, with and without flags.

    Runs pi's own loader modules (``resource-loader.js``,
    ``skills.js``) via node so the measurement reflects exactly what pi
    injects into the system prompt. Returns per-variant byte counts:
    ``{"with_flags": {"bytes": int, "skills_bytes": int},
        "without_flags": {...}}``.
    """
    variants = {}
    flags_json = json.dumps({"noContextFiles": True, "noSkills": True})
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as fh:
        fh.write(LOADER_MEASUREMENT_MJS)
        tmp_path = fh.name
    try:
        proc = subprocess.run(
            ["node", tmp_path, str(cwd), str(agent_dir) if agent_dir else "", flags_json],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"node measurement failed: {proc.stderr[-500:]}"
            )
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        variants["without_flags"] = {
            "bytes": int(out["without_flags"]["bytes"]),
            "skills": int(out["without_flags"]["skills"]),
        }
        variants["with_flags"] = {
            "bytes": int(out["with_flags"]["bytes"]),
            "skills": int(out["with_flags"]["skills"]),
        }
    finally:
        os.unlink(tmp_path)
    return variants


# ---------------------------------------------------------------------------
# Session-file token capture (AC2, empirical)
# ---------------------------------------------------------------------------

@dataclass
class SessionSample:
    item_id: str
    input_tokens: int
    session_file: str
    ts: str = ""


def _find_session_dirs() -> list[Path]:
    """Return pi session directories that hold SorraAgents project sessions."""
    base = Path.home() / ".pi" / "agent" / "sessions"
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and "SorraAgents" in p.name)


def collect_session_tokens(min_items: int = 5, max_items: int | None = None,
                           since: str | None = None) -> list[SessionSample]:
    """Extract first-call input tokens from audit-runner sessions.

    Scans pi session JSONL files, keeps sessions whose first user prompt
    carries an audit marker, extracts the work item id and the first
    assistant message's ``usage.input``, and returns one sample per
    (item, session). Sessions are deduplicated per item by taking the
    earliest timestamp, then capped at ``max_items`` distinct items
    (default: unlimited). When *since* (ISO date, e.g. ``2026-08-07``) is
    given, only sessions starting on or after that date are considered —
    this scopes the bound to sessions created after the context-reduction
    change landed. Raises ``RuntimeError`` if fewer than ``min_items``
    distinct items are found.
    """
    since_date = None
    if since:
        since_date = datetime.fromisoformat(since).replace(tzinfo=None)
    samples: list[SessionSample] = []
    seen_items: set[str] = set()

    for sess_dir in _find_session_dirs():
        for jsonl in sorted(sess_dir.glob("*.jsonl")):
            # Filename carries the session start time (UTC):
            # YYYY-MM-DDTHH-MM-SS-msZ_<uuid>.jsonl
            m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}-\d{2}-\d{2})", jsonl.name)
            if since_date is not None:
                if not m:
                    continue
                try:
                    file_dt = datetime.strptime(m.group(1) + "T" + m.group(2) + "+00:00", "%Y-%m-%dT%H-%M-%S%z").replace(tzinfo=None)
                except ValueError:
                    continue
                if file_dt < since_date:
                    continue
            item_id: str | None = None
            is_audit = False
            first_input: int | None = None
            ts = ""
            try:
                with open(jsonl, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if d.get("type") != "message":
                            continue
                        msg = d.get("message", {})
                        if not ts:
                            ts = d.get("timestamp") or d.get("time") or ""
                        content = msg.get("content")
                        text = ""
                        if isinstance(content, str):
                            text = content
                        elif isinstance(content, list):
                            text = " ".join(
                                p.get("text", "")
                                for p in content
                                if isinstance(p, dict) and p.get("type") == "text"
                            )
                        head = text[:2000]
                        if not is_audit and any(
                            head.startswith(m) for m in AUDIT_PROMPT_PREFIXES
                        ):
                            is_audit = True
                        if item_id is None:
                            m = WORK_ITEM_RE.search(head)
                            if m:
                                item_id = m.group(0)
                        usage = msg.get("usage")
                        if (
                            usage
                            and msg.get("role") == "assistant"
                            and isinstance(usage.get("input"), int)
                            and first_input is None
                        ):
                            first_input = usage["input"]
                        if is_audit and item_id and first_input is not None:
                            break
            except OSError:
                continue
            if is_audit and item_id and first_input is not None and item_id not in seen_items:
                seen_items.add(item_id)
                if max_items and len(seen_items) > max_items:
                    break
                samples.append(SessionSample(
                    item_id=item_id, input_tokens=first_input,
                    session_file=str(jsonl), ts=ts,
                ))
        if max_items and len(seen_items) >= max_items:
            break

    if len(samples) < min_items:
        raise RuntimeError(
            f"found {len(samples)} distinct audited item session(s); "
            f"need at least {min_items}"
        )
    return samples


def check_sessions(min_items: int, max_items: int | None, since: str | None = None) -> dict:
    """AC2 empirical check over real session files."""
    samples = collect_session_tokens(min_items=min_items, max_items=max_items, since=since)
    results = []
    violations = []
    for s in samples:
        ok = s.input_tokens < TOKEN_BOUND
        results.append({
            "item_id": s.item_id,
            "input_tokens": s.input_tokens,
            "under_10k": ok,
            "session_file": s.session_file,
            "ts": s.ts,
        })
        if not ok:
            violations.append(s.item_id)
    passed = not violations and len(samples) >= min_items
    return {
        "check": "sessions",
        "passed": passed,
        "distinct_items": len(samples),
        "min_items": min_items,
        "since": since,
        "samples": results,
        "violations": violations,
    }


# ---------------------------------------------------------------------------
# Re-audit sample (AC3)
# ---------------------------------------------------------------------------

@dataclass
class AuditItem:
    item_id: str
    pre_status: str | None = None
    pre_stage: str | None = None
    pre_assignee: str | None = None
    persisted_verdict: str | None = None
    persisted_time: str | None = None


@dataclass
class ReauditResult:
    item_id: str
    persisted_verdict: str | None = None
    flags_on_verdict: str | None = None
    flags_off_verdict: str | None = None
    input_tokens: list[int] = field(default_factory=list)
    divergence: str | None = None
    passed: bool = False


def _stage_for_audit(item: AuditItem) -> None:
    """Move a closed item into a state the runner's lifecycle accepts.

    The runner sets ``--status in_progress`` on entry without a stage;
    the worklog lifecycle rejects that for ``stage: done`` items, so
    closed items must be pre-staged to ``in_progress/in_progress`` first.
    ``_restore_item_state`` returns them to the captured pre-audit state.
    """
    if item.pre_status == "completed" and item.pre_stage == "done":
        try:
            _wl(["wl", "update", item.item_id,
                 "--status", "in_progress", "--stage", "in_progress"],
                timeout=60)
        except Exception:  # noqa: BLE001, S110 -- best-effort; runner reports
            pass


def _run_audit(runner_path: Path, item_id: str, repo_root: Path,
               timeout_min: int = 30, item: AuditItem | None = None) -> subprocess.CompletedProcess:
    """Run one audit of *item_id* with ``--force --do-not-persist``.

    Returns the completed process; callers read ``stdout``/``stderr``.
    When *item* is given and closed, it is pre-staged so the runner's
    status lifecycle can set in_progress (see :func:`_stage_for_audit`).
    """
    if item is not None:
        _stage_for_audit(item)
    return subprocess.run(
        [
            sys.executable, str(runner_path), "issue", item_id,
            "--force", "--do-not-persist",
        ],
        cwd=str(repo_root),
        capture_output=True, text=True,
        timeout=timeout_min * 60,
        check=False,
    )


def _parse_verdict(proc: subprocess.CompletedProcess) -> tuple[str | None, list[int]]:
    """Extract the Ready-to-close verdict and per-call input_tokens."""
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    m = READY_RE.search(text)
    verdict = m.group(1) if m else None
    tokens = [int(g[3]) for g in TIMING_LINE_RE.findall(text) if g[3]]
    return verdict, tokens


def _flag_off_runner_copy(runner_path: Path, repo_root: Path, tmp_dir: Path) -> Path:
    """Create a runner copy with the context-reduction flags removed.

    Removes the ``cmd.extend(["--no-context-files", "--no-skills"])`` line
    and pins ``REPO_ROOT`` to *repo_root* (the copy lives outside the repo
    so ``parents[3]`` would otherwise resolve wrong).
    """
    src = runner_path.read_text(encoding="utf-8")
    src = re.sub(
        r"cmd\.extend\(\[\"--no-context-files\", \"--no-skills\"\]\)\n",
        "",
        src,
    )
    src = re.sub(
        r"_SKILLS_ROOT = Path\(__file__\)\.resolve\(\)\.parents\[2\]",
        f"_SKILLS_ROOT = Path({str(repo_root)!r})",
        src,
    )
    copy = tmp_dir / "audit_runner_noflags.py"
    copy.write_text(src, encoding="utf-8")
    return copy


def _wl_flags() -> list[str]:
    """Resolve ``--worklog-dir`` flags for wl subprocess calls.

    Mirrors audit_runner's ``_resolve_worklog_flags`` so wl commands target
    the correct worklog store regardless of the caller's cwd (worktrees
    have no worklog DB of their own).
    """
    try:
        _SKILLS_ROOT = Path(__file__).resolve().parents[2]
        if str(_SKILLS_ROOT) not in sys.path:
            sys.path.insert(0, str(_SKILLS_ROOT))
        from shared.status_lifecycle import (  # type: ignore[import-not-found]
            resolve_worklog_flags as _resolve,
        )
        return _resolve(["wl", "show", "--json"])
    except Exception:  # noqa: BLE001 -- fall back to no flags
        return []


def _wl(cmd: list[str], timeout: int = 120) -> dict:
    """Run a ``wl`` command, injecting worklog flags, and return parsed JSON."""
    full = list(cmd)
    if full and full[0] == "wl":
        full[1:1] = _wl_flags()
    proc = subprocess.run(full, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"wl command failed ({' '.join(full)}): {proc.stderr[-300:]} "
            f"{proc.stdout[-300:]}"
        )
    return json.loads(proc.stdout)


def _fetch_item_state(item_id: str) -> AuditItem:
    """Read persisted audit + current status/stage from wl."""
    item = AuditItem(item_id=item_id)
    try:
        d = _wl(["wl", "show", item_id, "--json"])
        wi = d.get("workItem", {})
        item.pre_status = wi.get("status")
        item.pre_stage = wi.get("stage")
        item.pre_assignee = wi.get("assignee")
        audit = wi.get("audit") or {}
        item.persisted_time = (audit.get("time") or "")[:10] or None
        m = READY_RE.search(audit.get("text") or "")
        if m:
            item.persisted_verdict = m.group(1)
    except Exception:  # noqa: BLE001, S110 -- best-effort parse
        pass
    return item


def _restore_item_state(item: AuditItem) -> None:
    """Restore the pre-audit status/stage (re-audits must not demote items)."""
    if item.pre_status and item.pre_stage:
        try:
            _wl(["wl", "update", item.item_id,
                 "--status", item.pre_status, "--stage", item.pre_stage],
                timeout=60)
        except Exception:  # noqa: BLE001, S110 -- restore is best-effort
            pass
    if item.pre_assignee:
        try:
            _wl(["wl", "update", item.item_id,
                 "--assignee", item.pre_assignee],
                timeout=60)
        except Exception:  # noqa: BLE001, S110 -- restore is best-effort
            pass


def _sample_audited_items(count: int, seed: int, repo_root: Path) -> list[str]:
    """Deterministic sample of previously-audited work items.

    Enumerates audited work items via per-status scoped ``wl list --status
    <s>`` queries piped through ``jq`` (SA-0MSLVQMKF000ESPZ): every query
    carries a ``--status`` scoping filter per AGENTS.md, and only the
    ``{id, auditedAt, description}`` projection crosses the unbounded OS
    pipe into the process buffer — the 5.3 MB full dump never enters
    memory. Audited items span all statuses, so the four statuses are
    queried and merged. Keeps those with a persisted audit record, sorts
    by id, and draws candidates with ``random.Random(seed)`` in a
    deterministic order. Probes candidates (via ``wl show``) until *count*
    items whose persisted audit text carries a runner verdict (``Ready to
    close: Yes|No``) are found, so the before/after cross-check has real
    persisted verdicts to compare against. Probing is bounded (20 probes
    per item wanted) to keep the enumeration fast.
    """
    items: list[dict] = []
    for status in ("open", "in-progress", "blocked", "completed"):
        proc = subprocess.run(
            ["bash", "-c",
             (f"wl list --status {status} --json "
              "| jq -c '[.workItems[] | {id, auditedAt, description}]'")],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if proc.returncode != 0:
            continue
        try:
            d = json.loads(proc.stdout)
        except json.JSONDecodeError:
            continue
        batch = d if isinstance(d, list) else (d.get("workItems") or d.get("items") or [])
        items.extend(batch)
    # Pre-filter: prefer items whose description carries acceptance criteria
    # so the re-audit exercises the parent pi call (and therefore captures
    # per-call input_tokens). Fall back to any audited item otherwise.
    def _has_acs(wi: dict) -> bool:
        desc = wi.get("description") or ""
        return bool(re.search(r"acceptance criter", desc, re.IGNORECASE)) and len(desc) > 300

    audited = sorted(
        wi["id"] for wi in items
        if isinstance(wi, dict) and wi.get("auditedAt")
    )
    audited_acs = [i for i in audited if _has_acs(next(w for w in items if w.get("id") == i))]
    pool = audited_acs if len(audited_acs) >= count else audited
    if len(pool) < count:
        raise RuntimeError(
            f"only {len(pool)} audited work items found; need {count}"
        )
    rng = random.Random(seed)
    # Shuffle deterministically, then walk the shuffled list in order.
    order = pool[:]
    rng.shuffle(order)
    picked: list[str] = []
    max_probes = count * 20
    for item_id in order:
        if len(picked) >= count:
            break
        max_probes -= 1
        if max_probes < 0:
            break
        try:
            state = _fetch_item_state(item_id)
        except Exception:  # noqa: BLE001, S112 -- skip unreadable items
            continue
        if state.persisted_verdict:
            picked.append(item_id)
    if len(picked) < count:
        raise RuntimeError(
            f"only {len(picked)} work items with a persisted runner verdict "
            f"found after bounded probing; need {count}"
        )
    return picked


def reaudit_sample(items: list[str], repo_root: Path, runner_path: Path,
                   controlled: bool = True, timeout_min: int = 30) -> dict:
    """AC3: re-audit a sample and compare verdicts before/after the change.

    The controlled comparison (default) re-audits each item twice — once
    with the current runner (context-reduction flags on) and once with a
    flag-off runner copy (the pre-change code path) — under identical
    conditions, isolating the change itself. The persisted pre-change
    verdict (if the item carries one) is reported as a cross-check.

    Returns a report dict; ``passed`` is True only when every item's
    flags-on verdict matches its flags-off verdict (controlled), or its
    persisted pre-change verdict (non-controlled), AND every captured
    per-call input token count is under the AC2 bound.
    """
    results: list[ReauditResult] = []
    overall_pass = True
    token_samples: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="verify-ctx-") as td:
        tmp = Path(td)
        noflags_runner = _flag_off_runner_copy(runner_path, repo_root, tmp) if controlled else None

        for item_id in items:
            state = _fetch_item_state(item_id)
            res = ReauditResult(
                item_id=item_id,
                persisted_verdict=state.persisted_verdict,
            )
            try:
                # Post-change path: current runner with flags.
                proc_on = _run_audit(runner_path, item_id, repo_root, timeout_min,
                                     item=state)
                verdict_on, tokens_on = _parse_verdict(proc_on)
                res.flags_on_verdict = verdict_on
                res.input_tokens = tokens_on
                token_samples.extend(
                    {"item_id": item_id, "input_tokens": t} for t in tokens_on
                )
                if tokens_on and max(tokens_on) >= TOKEN_BOUND:
                    res.divergence = (
                        f"input_tokens={max(tokens_on)} exceeds {TOKEN_BOUND} (AC2)"
                    )
                elif controlled and noflags_runner is not None:
                    # Pre-change path: flag-off runner copy.
                    proc_off = _run_audit(noflags_runner, item_id, repo_root,
                                           timeout_min, item=state)
                    verdict_off, _ = _parse_verdict(proc_off)
                    res.flags_off_verdict = verdict_off
                    if verdict_on != verdict_off:
                        res.divergence = (
                            f"verdict differs with/without flags: "
                            f"on={verdict_on} off={verdict_off}"
                        )
                else:
                    # Compare against the persisted pre-change verdict.
                    if state.persisted_verdict and verdict_on != state.persisted_verdict:
                        res.divergence = (
                            f"verdict differs from persisted pre-change verdict: "
                            f"persisted={state.persisted_verdict} now={verdict_on}"
                        )
                    elif not state.persisted_verdict:
                        res.divergence = "no persisted pre-change verdict to compare"
            except subprocess.TimeoutExpired:
                res.divergence = "audit timed out (no verdict)"
            except Exception as exc:  # noqa: BLE001 -- report, don't crash
                res.divergence = f"error: {exc}"
            finally:
                _restore_item_state(state)

            res.passed = res.divergence is None
            overall_pass = overall_pass and res.passed
            results.append(res)

    return {
        "check": "reaudit-sample",
        "passed": overall_pass,
        "controlled": controlled,
        "items": len(results),
        "results": [vars(r) for r in results],
        "token_samples": token_samples,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _markdown(report: dict) -> str:
    lines = [
        f"# Context-reduction verification ({report['check']})",
        "",
        f"Timestamp: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Passed: **{report['passed']}**",
        "",
    ]
    if report["check"] == "sessions":
        lines += [
            (f"Distinct audited items sampled: {report['distinct_items']} "
             f"(min required: {report['min_items']})"),
            f"Session window: since {report.get('since') or 'all'}",
            "",
            "| item | first-call input tokens | < 10K | session |",
            "|---|---|---|---|",
        ]
        for s in report["samples"]:
            lines.append(
                f"| {s['item_id']} | {s['input_tokens']} | {s['under_10k']} | "
                f"`{s['session_file']}` |"
            )
    elif report["check"] == "static":
        for variant in ("without_flags", "with_flags"):
            v = report[variant]
            est = v["bytes"] / BYTES_PER_TOKEN_ESTIMATE
            lines.append(
                f"- {variant}: {v['bytes']} bytes "
                f"(~{est:.0f} tokens est., {BYTES_PER_TOKEN_ESTIMATE:.1f} B/token)"
            )
        lines.append("")
        lines.append(
            f"Reduction: {report['reduction_bytes']} bytes "
            f"({report['reduction_pct']}%)"
        )
        lines.append("")
        lines.append(f"Bound check (< {TOKEN_BOUND} tokens): {report['passed']}")
    elif report["check"] == "reaudit-sample":
        lines += ["", "| item | persisted (pre-change) | flags-on | flags-off | input_tokens | status | divergence |", "|---|---|---|---|---|---|---|"]
        for r in report["results"]:
            lines.append(
                f"| {r['item_id']} | {r['persisted_verdict'] or '-'} | "
                f"{r['flags_on_verdict'] or '-'} | {r['flags_off_verdict'] or '-'} | "
                f"{r['input_tokens']} | {'PASS' if r['passed'] else 'DIVERGE'} | "
                f"{r.get('divergence') or ''} |"
            )
        if report.get("token_samples"):
            lines.append("")
            lines.append("Per-call input tokens (AC2):")
            lines.append("")
            for t in report["token_samples"]:
                lines.append(f"- {t['item_id']}: {t['input_tokens']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("check-static", help="AC2 deterministic static-context bound")
    s.set_defaults(func=cmd_check_static)

    s = sub.add_parser("check-sessions", help="AC2 empirical session-token capture")
    s.add_argument("--min-items", type=int, default=5,
                   help="min distinct audited items required (default 5)")
    s.add_argument("--max-items", type=int, default=None,
                   help="cap distinct items sampled")
    s.add_argument("--since", default="2026-08-07T11:15",
                   help="only sessions starting on/after this ISO timestamp (UTC); "
                        "default 2026-08-07T11:15 = when the context-reduction "
                        "flags landed in commit 8efc7172)")
    s.set_defaults(func=cmd_check_sessions)

    s = sub.add_parser("reaudit-sample", help="AC3 re-audit + verdict comparison")
    s.add_argument("--items", nargs="+", default=None,
                   help="explicit work item ids (default: deterministic sample)")
    s.add_argument("--count", type=int, default=5,
                   help="sample size when --items not given (default 5)")
    s.add_argument("--seed", type=int, default=42,
                   help="sampling seed (default 42)")
    s.add_argument("--controlled", action="store_true", default=True,
                   help="re-audit each item with a flag-off runner copy and compare "
                        "verdicts (AC3 controlled before/after; default ON)")
    s.add_argument("--no-controlled", dest="controlled", action="store_false",
                   help="compare against persisted verdicts only")
    s.add_argument("--timeout-min", type=int, default=30,
                   help="per-audit timeout in minutes (default 30)")
    s.set_defaults(func=cmd_reaudit)

    p.add_argument("--runner", default=None,
                   help="path to audit_runner.py (default: alongside this script)")
    p.add_argument("--repo-root", default=None,
                   help="project root (default: cwd)")
    p.add_argument("--agent-dir", default=None,
                   help="agent dir for the static measurement (default: ~/.pi/agent)")
    p.add_argument("--report-dir", default=None,
                   help="directory for report.json/report.md (default: none)")
    return p


def _runner_path(cli_value: str | None) -> Path:
    if cli_value:
        return Path(cli_value).resolve()
    return Path(__file__).resolve().parent / "audit_runner.py"


def cmd_check_static(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root or os.getcwd()).resolve()
    agent_dir = Path(args.agent_dir or (Path.home() / ".pi" / "agent")).resolve()
    meas = measure_static_context(repo_root, agent_dir)
    with_f = meas["with_flags"]["bytes"]
    without_f = meas["without_flags"]["bytes"]
    with_est = with_f / BYTES_PER_TOKEN_ESTIMATE
    passed = with_est < TOKEN_BOUND
    report = {
        "check": "static",
        "passed": passed,
        "bound_tokens": TOKEN_BOUND,
        "bytes_per_token_estimate": BYTES_PER_TOKEN_ESTIMATE,
        "without_flags": meas["without_flags"],
        "with_flags": meas["with_flags"],
        "token_estimate_with_flags": round(with_est, 1),
        "reduction_bytes": without_f - with_f,
        "reduction_pct": round(100 * (without_f - with_f) / without_f, 1) if without_f else 0.0,
    }
    _emit(report, args.report_dir)
    print(_markdown(report))
    return 0 if passed else 1


def cmd_check_sessions(args: argparse.Namespace) -> int:
    try:
        report = check_sessions(min_items=args.min_items, max_items=args.max_items,
                                since=args.since)
    except RuntimeError as exc:
        print(f"check-sessions FAILED: {exc}", file=sys.stderr)
        return 1
    _emit(report, args.report_dir)
    print(_markdown(report))
    return 0 if report["passed"] else 1


def cmd_reaudit(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root or os.getcwd()).resolve()
    runner = _runner_path(args.runner)
    if not runner.is_file():
        print(f"runner not found: {runner}", file=sys.stderr)
        return 2
    items: list[str]
    if args.items:
        items = args.items
    else:
        try:
            items = _sample_audited_items(args.count, args.seed, repo_root)
        except Exception as exc:  # noqa: BLE001
            print(f"failed to sample audited items: {exc}", file=sys.stderr)
            return 2
    report = reaudit_sample(items, repo_root, runner,
                            controlled=args.controlled,
                            timeout_min=args.timeout_min)
    _emit(report, args.report_dir)
    print(_markdown(report))
    return 0 if report["passed"] else 1


def _emit(report: dict, report_dir: str | None) -> None:
    if not report_dir:
        return
    d = Path(report_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    (d / "report.md").write_text(_markdown(report), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
