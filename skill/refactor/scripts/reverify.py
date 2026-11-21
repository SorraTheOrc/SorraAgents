#!/usr/bin/env python3
"""Re-verification sweep for linter-derived work items (SA-0MW7Z1JCE009G6J3).

Nothing used to re-check a linter-derived work item against the current tree.
Once filed, a refactor or audit finding stayed open until somebody happened to
look at it — even after the target file was deleted or the diagnostic had long
stopped firing. This sweep closes that gap.

For every item attributable to a linter family it:

1. recovers the exact verification command and content fingerprint recorded at
   creation (``Refactor-Verification-Command:`` / ``Audit-Verification-Command:``
   plus the matching ``…-Fingerprint:`` marker), falling back to deriving
   ``ruff check --isolated --select <RULE> <path>`` from the item's finding
   identity when an older item predates the metadata;
2. re-runs the command and classifies the item as ``still-reproduces``,
   ``no-longer-reproduces`` or ``target-gone``;
3. on ``--apply`` closes ``target-gone`` / ``no-longer-reproduces`` items with
   an evidence comment and a recorded rationale — never silently.

Safety contract:

- **Dry-run by default** — without ``--apply`` the sweep makes ZERO worklog
  mutations and only reports what it would do.
- **Attribution only** — an item is touched only when it can be attributed to
  a linter family: the ``Refactor`` tag with a ``Refactor-Finding-Key`` /
  ``Refactor: … in <path>`` title, a ``[SEVERITY] <path>:<line> — … (<RULE>)``
  code-quality title, or a ``ruff <RULE> in <path>`` / ``CFP-Chore-Key`` audit
  chore. Human-authored ``bug``/``feature``/``epic`` items are never touched.
- **``still-reproduces`` is left untouched** — a live finding keeps its item
  and gains no comment (evidence is reported in the run output instead).
- **Idempotent** — closed items become terminal and are skipped on the next
  run, so a scheduled sweep converges.

Usage::

    python3 $(skill_path refactor)/scripts/reverify.py            # dry run
    python3 $(skill_path refactor)/scripts/reverify.py --apply    # close stale
    python3 $(skill_path refactor)/scripts/reverify.py --json     # machine output

Scheduling: the command is safe to run from cron (dry-run by default; add
``--apply`` only where automated closure is intended). See
``skill/refactor/SKILL.md`` for the documented hook.

Related work items:
- SA-0MW7Z1JCE009G6J3 (this sweep)
- SA-0MWE1E4GQ004E1JG (finding identity + fingerprint markers)
- SA-0MVZNECSD005XB42 (audit candidate-false-positive chores)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Add ``skill/`` to sys.path so ``refactor.*`` imports resolve both when this
# module is imported as ``refactor.scripts.reverify`` (tests) and when it is
# run directly from the repository (CLI). Mirrors the bootstrap in
# ``consolidate_duplicates.py``.
_SKILLS_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_ROOT_STR = str(_SKILLS_ROOT)
if _SKILLS_ROOT_STR in sys.path:
    sys.path.remove(_SKILLS_ROOT_STR)
sys.path.insert(0, _SKILLS_ROOT_STR)

LOG = logging.getLogger("refactor.reverify")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REFACTOR_TAG = "Refactor"
AUDIT_FP_TITLE_MARKER = "candidate false positive"

TERMINAL_STATUSES = frozenset({"completed", "deleted"})

# Human-authored item types are NEVER re-verified or closed. A refactor/audit
# finding is a task/chore; a bug/feature/epic is real work a person filed.
HUMAN_AUTHORED_ISSUE_TYPES = frozenset({"bug", "feature", "epic"})

EPHEMERAL_WORKTREE_PREFIX = ".worklog/worktrees"

CLASS_STILL = "still-reproduces"
CLASS_FIXED = "no-longer-reproduces"
CLASS_GONE = "target-gone"
CLASS_UNVERIFIABLE = "unverifiable"
CLASSIFICATIONS = (CLASS_STILL, CLASS_FIXED, CLASS_GONE, CLASS_UNVERIFIABLE)

#: Classifications on which ``--apply`` acts (closes the item).
ACTIONABLE_CLASSIFICATIONS = frozenset({CLASS_FIXED, CLASS_GONE})

REVERIFY_AUTHOR = "refactor-reverify"
EVIDENCE_MARKER = "Reverify-Evidence:"

# Identity / metadata markers (refactor and audit families).
_REFACTOR_KEY_RE = re.compile(r"Refactor-Finding-Key:[ \t]*(\S+)", re.IGNORECASE)
_CFP_KEY_RE = re.compile(r"CFP-Chore-Key:[ \t]*(\S+)", re.IGNORECASE)
_VERIFY_CMD_RE = re.compile(
    r"(?:Refactor|Audit)-Verification-Command:[ \t]*(.+?)[ \t]*$",
    re.MULTILINE,
)
_FINGERPRINT_RE = re.compile(
    r"(?:Refactor|Audit)-Fingerprint:[ \t]*(\S+)", re.IGNORECASE
)

# Refactor-family title: ``Refactor: <Smell Type> in <path>``.
_REFACTOR_TITLE_RE = re.compile(r"^Refactor:\s+.+\s+in\s+(.+?)\s*$", re.IGNORECASE)

# Audit-family title: ``ruff <RULE> in <path>[:<line>] — candidate false
# positive …``. The line (when present) is excluded from the identity.
_AUDIT_TITLE_RE = re.compile(
    r"^(?P<linter>\S+)\s+(?P<code>\S+)\s+in\s+(?P<path>.+?)(?::\d+)?\s*—\s*"
    r"candidate false positive",
    re.IGNORECASE,
)

# Code-quality title: ``[SEVERITY] <path>:<line> — <message> (<CODE>)``.
_QUALITY_TITLE_RE = re.compile(
    r"^\[[^\]]+\]\s+(?P<path>.+?):(?P<line>\d+)\s+—\s+.*"
    r"\((?P<code>[A-Za-z0-9_]+)\)\s*$"
)

# Legacy (pre-marker) structured fields for older Refactor items.
_LEGACY_CODE_RE = re.compile(r"\*\*Code\*\*\s*[:\s]+`?([^`\n]+)`?")
_LEGACY_FILE_RE = re.compile(r"\*\*File\*\*\s*[:\s]+`?([^`\n]+)`?")

# Ruff rule codes are an upper-case letter prefix followed by digits
# (``S112``, ``SIM117``, ``PLW1510``).
_RUFF_CODE_RE = re.compile(r"^[A-Z]{1,6}[0-9]{1,4}$")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class FindingEvidence:
    """The identity + recorded metadata recovered from one work item."""

    family: str
    code: str = ""
    path: str = ""
    linter: str = ""
    line: int = 0
    verification_command: str = ""
    fingerprint: str = ""


@dataclass
class Classification:
    """One item's re-verification outcome."""

    item_id: str
    title: str
    family: str
    classification: str = CLASS_UNVERIFIABLE
    code: str = ""
    path: str = ""
    linter: str = ""
    command: str = ""
    hit_count: int = 0
    reason: str = ""
    recorded_fingerprint: str = ""
    current_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "title": self.title,
            "family": self.family,
            "classification": self.classification,
            "code": self.code,
            "path": self.path,
            "linter": self.linter,
            "command": self.command,
            "hit_count": self.hit_count,
            "reason": self.reason,
            "recorded_fingerprint": self.recorded_fingerprint,
            "current_fingerprint": self.current_fingerprint,
        }


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _strip_worktree_prefix(path: str) -> str:
    """Drop a ``/.worklog/worktrees/<wt>/`` prefix from a path (pure)."""
    marker = f"/{EPHEMERAL_WORKTREE_PREFIX}/"
    idx = path.find(marker)
    if idx >= 0:
        rest = path[idx + len(marker):]
        slash = rest.find("/")
        if slash >= 0:
            return rest[slash + 1:]
    return path


def normalise_path(path: str, repo_root: str | None = None) -> str:
    """Normalise *path* to repository-relative POSIX form.

    A worktree-recorded absolute path and a main-checkout relative path for
    the same file collapse to one identity, so re-verification never splits a
    finding (SA-0MW7Z11PV003V7O2).
    """
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return ""
    text = _strip_worktree_prefix(text)
    if os.path.isabs(text) and repo_root:
        try:
            text = os.path.relpath(text, repo_root)
        except (ValueError, OSError):
            pass
    while text.startswith("./"):
        text = text[2:]
    if text not in ("/", ""):
        text = text.rstrip("/")
    return text


def is_ephemeral_worktree_path(path: str) -> bool:
    """Whether *path* lies under ``.worklog/worktrees`` (never actionable)."""
    rel = normalise_path(path)
    return rel == EPHEMERAL_WORKTREE_PREFIX or rel.startswith(
        EPHEMERAL_WORKTREE_PREFIX + "/"
    )


def resolve_repo_root(explicit: str | None = None) -> str:
    """Resolve the main checkout root (stable from a linked worktree too).

    ``git rev-parse --git-common-dir`` returns the shared main ``.git``
    directory whether the process runs in the main checkout or a linked
    worktree, so its parent is the stable base a repository-relative path
    resolves against.
    """
    if explicit:
        return os.path.abspath(explicit)
    root: str | None = None
    try:
        common = subprocess.run(  # noqa: PLW1510
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=10,
        )
        common_dir = (common.stdout or "").strip() if common.returncode == 0 else ""
        if common_dir:
            if os.path.isabs(common_dir):
                root = os.path.dirname(common_dir)
            else:
                top = subprocess.run(  # noqa: PLW1510
                    ["git", "rev-parse", "--show-toplevel"],
                    capture_output=True, text=True, timeout=10,
                )
                top_dir = (top.stdout or "").strip() if top.returncode == 0 else ""
                if top_dir:
                    root = os.path.normpath(
                        os.path.join(top_dir, common_dir, os.pardir)
                    )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        root = None
    return os.path.abspath(root or os.getcwd())


# ---------------------------------------------------------------------------
# Metadata extraction (pure)
# ---------------------------------------------------------------------------


def _extract_verification_command(haystack: str) -> str:
    match = _VERIFY_CMD_RE.search(haystack or "")
    return match.group(1).strip() if match else ""


def _extract_fingerprint(haystack: str) -> str:
    match = _FINGERPRINT_RE.search(haystack or "")
    return match.group(1).strip() if match else ""


def _parse_key(key: str) -> tuple[str, str]:
    """Split ``<code>:<path>`` on the first colon only."""
    text = str(key or "").strip()
    if ":" in text:
        code, path = text.split(":", 1)
    else:
        code, path = text, ""
    return code.strip(), path.strip()


def attribute_item(
    item: dict[str, Any], repo_root: str | None = None
) -> FindingEvidence | None:
    """Recover the linter finding identity from *item*, or ``None``.

    Returning ``None`` means the item is not attributable to a linter family
    (a human-authored bug/feature, a generic note, a terminal item) and must
    never be touched by the sweep.
    """
    if not isinstance(item, dict):
        return None
    status = str(item.get("status") or "").strip().lower()
    if status in TERMINAL_STATUSES:
        return None
    issue_type = str(item.get("issueType") or "").strip().lower()
    if issue_type in HUMAN_AUTHORED_ISSUE_TYPES:
        return None

    title = str(item.get("title") or "")
    description = str(item.get("description") or "")
    haystack = f"{title}\n{description}"
    tags = {
        str(tag).strip().lower() for tag in (item.get("tags") or [])
    }

    # 1. Code-quality review title: ``[SEVERITY] path:line — msg (CODE)``.
    quality = _QUALITY_TITLE_RE.match(title.strip())
    if quality:
        evidence = FindingEvidence(
            family="quality",
            code=quality.group("code").strip(),
            path=normalise_path(quality.group("path"), repo_root),
            line=int(quality.group("line") or 0),
            verification_command=_extract_verification_command(haystack),
            fingerprint=_extract_fingerprint(haystack),
        )
        if _RUFF_CODE_RE.match(evidence.code.upper()):
            evidence.linter = "ruff"
        return evidence

    # 2. Audit candidate-false-positive chore (marker or title).
    cfp_key = _CFP_KEY_RE.search(haystack)
    audit_title = _AUDIT_TITLE_RE.match(title.strip())
    if cfp_key or audit_title:
        evidence = FindingEvidence(
            family="audit",
            verification_command=_extract_verification_command(haystack),
            fingerprint=_extract_fingerprint(haystack),
        )
        if cfp_key:
            parts = cfp_key.group(1).split(":", 3)
            if len(parts) >= 3:
                evidence.linter = parts[0].strip().lower()
                evidence.code = parts[1].strip()
                evidence.path = normalise_path(parts[2], repo_root)
                if len(parts) == 4 and parts[3].strip().isdigit():
                    evidence.line = int(parts[3].strip())
        if not evidence.path and audit_title:
            evidence.linter = audit_title.group("linter").strip().lower()
            evidence.code = audit_title.group("code").strip()
            evidence.path = normalise_path(audit_title.group("path"), repo_root)
        if not evidence.linter and _RUFF_CODE_RE.match(evidence.code.upper()):
            evidence.linter = "ruff"
        return evidence

    # 3. Refactor-family item (marker, Refactor tag, or title).
    refactor_key = _REFACTOR_KEY_RE.search(haystack)
    if not refactor_key and "refactor" not in tags:
        return None

    evidence = FindingEvidence(family="refactor")
    if refactor_key:
        code, path = _parse_key(refactor_key.group(1))
        evidence.code = code
        evidence.path = normalise_path(path, repo_root)
    else:
        title_match = _REFACTOR_TITLE_RE.match(title.strip())
        if not title_match:
            # A Refactor-tagged item without a finding title (e.g. a planning
            # or documentation task) is not a linter finding.
            return None
        evidence.path = normalise_path(title_match.group(1), repo_root)
        code_match = _LEGACY_CODE_RE.search(description)
        if code_match:
            evidence.code = code_match.group(1).strip().strip("`")
        file_match = _LEGACY_FILE_RE.search(description)
        if file_match:
            evidence.path = normalise_path(
                file_match.group(1).strip().strip("`"), repo_root
            )
    if not evidence.path:
        return None
    evidence.verification_command = _extract_verification_command(haystack)
    evidence.fingerprint = _extract_fingerprint(haystack)
    if _RUFF_CODE_RE.match(evidence.code.upper()):
        evidence.linter = "ruff"
    return evidence


# ---------------------------------------------------------------------------
# Command execution / result parsing
# ---------------------------------------------------------------------------


def _run_command(
    argv: Sequence[str], cwd: str, timeout: int = 120
) -> subprocess.CompletedProcess:
    """Execute *argv* in *cwd*; a missing binary becomes a synthetic result."""
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(
            args=list(argv), returncode=127, stdout="", stderr=str(exc)
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return subprocess.CompletedProcess(
            args=list(argv), returncode=124, stdout="", stderr=str(exc)
        )


def _with_json_output(argv: Sequence[str]) -> list[str]:
    """Return *argv* with ``--output-format json`` so results parse cleanly."""
    safe = list(argv)
    if "--output-format" in safe:
        idx = safe.index("--output-format")
        if idx + 1 < len(safe):
            safe[idx + 1] = "json"
        return safe
    safe.extend(["--output-format", "json"])
    return safe


def count_ruff_hits(stdout: str, code: str) -> int:
    """Count ruff findings in *stdout* matching *code* (JSON or text output)."""
    text = (stdout or "").strip()
    if not text:
        return 0
    upper = (code or "").strip().upper()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        data = None
    if isinstance(data, list):
        if not upper:
            return len(data)
        return sum(
            1
            for finding in data
            if isinstance(finding, dict)
            and str(finding.get("code") or "").upper() == upper
        )
    lines = [line for line in text.splitlines() if line.strip()]
    if not upper:
        return len(lines)
    return sum(1 for line in lines if upper in line.upper())


def content_fingerprint(abs_path: str, code: str) -> str:
    """sha256 of a file's bytes + ``NUL`` + rule code (best-effort)."""
    try:
        with open(abs_path, "rb") as handle:
            content = handle.read()
    except (OSError, ValueError):
        return ""
    digest = hashlib.sha256()
    digest.update(content)
    digest.update(b"\x00")
    digest.update(str(code or "").encode("utf-8"))
    return digest.hexdigest()


def derive_verification_command(evidence: FindingEvidence) -> str:
    """Derive a ruff command when an item predates the recorded metadata."""
    if not _RUFF_CODE_RE.match((evidence.code or "").upper()):
        return ""
    if not evidence.path:
        return ""
    return (
        f"ruff check --isolated --select {evidence.code.upper()} {evidence.path}"
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_item(
    item: dict[str, Any],
    repo_root: str,
    run_command: Callable[[Sequence[str], str], subprocess.CompletedProcess]
    | None = None,
    timeout: int = 120,
) -> Classification | None:
    """Classify one work item as still/fixed/gone/unverifiable.

    Returns ``None`` when the item is not attributable to a linter family.
    """
    evidence = attribute_item(item, repo_root)
    if evidence is None:
        return None

    run = run_command or _run_command
    result = Classification(
        item_id=str(item.get("id") or ""),
        title=str(item.get("title") or ""),
        family=evidence.family,
        code=evidence.code,
        path=evidence.path,
        linter=evidence.linter,
        recorded_fingerprint=evidence.fingerprint,
    )

    if not evidence.path:
        result.reason = "no target path recovered from the item"
        return result
    if is_ephemeral_worktree_path(evidence.path):
        result.reason = "target lies under .worklog/worktrees (ephemeral)"
        return result

    abs_path = (
        evidence.path
        if os.path.isabs(evidence.path)
        else os.path.join(repo_root, evidence.path)
    )
    if not os.path.isfile(abs_path):
        result.classification = CLASS_GONE
        result.reason = f"target file not found: {evidence.path}"
        return result

    result.current_fingerprint = content_fingerprint(abs_path, evidence.code)

    command = evidence.verification_command or derive_verification_command(
        evidence
    )
    result.command = command
    if not command:
        result.reason = (
            "no verification command recorded and none could be derived "
            "(unsupported linter or missing rule code)"
        )
        return result

    argv = shlex.split(command)
    if not argv or "ruff" not in os.path.basename(argv[0]).lower():
        result.reason = f"unsupported verification command: {command}"
        return result

    proc = run(_with_json_output(argv), repo_root)
    result.hit_count = 0
    if proc.returncode not in (0, 1):
        result.reason = (
            f"verification command failed (exit {proc.returncode}): "
            f"{(proc.stderr or '').strip()[:200]}"
        )
        return result

    hits = count_ruff_hits(proc.stdout, evidence.code)
    result.hit_count = hits
    if hits > 0:
        result.classification = CLASS_STILL
        result.reason = f"{hits} finding(s) still reported"
    else:
        result.classification = CLASS_FIXED
        result.reason = "0 findings reported"
    return result


# ---------------------------------------------------------------------------
# Worklog access
# ---------------------------------------------------------------------------


def _wl_command(
    cmd: Sequence[str], worklog_dir: str | None = None
) -> list[str]:
    argv = list(cmd)
    if worklog_dir:
        argv.extend(["--worklog-dir", worklog_dir])
    return argv


def _run_wl(
    cmd: Sequence[str], worklog_dir: str | None = None
) -> dict[str, Any] | None:
    """Run a ``wl`` command and return parsed JSON (``None`` on failure)."""
    argv = _wl_command(cmd, worklog_dir)
    try:
        result = subprocess.run(  # noqa: PLW1510
            argv, capture_output=True, text=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        LOG.warning("wl command failed (%s): %s", " ".join(argv), exc)
        return None
    if result.returncode != 0:
        LOG.warning(
            "wl command failed (code %d): %s",
            result.returncode, result.stderr.strip(),
        )
        return None
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        LOG.warning(
            "wl command returned unparseable output: %s", result.stdout[:200]
        )
        return None
    return data if isinstance(data, dict) else None


def _items_from(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    items = data.get("workItems", data.get("data", []))
    return [item for item in items if isinstance(item, dict)] if isinstance(
        items, list
    ) else []


def load_candidates(
    worklog_dir: str | None = None,
    wl_run: Callable[[Sequence[str], str | None], dict[str, Any] | None]
    | None = None,
) -> list[dict[str, Any]]:
    """Load every candidate item from the three attributable linter families.

    Three read-only lookups (``Refactor`` tag, ``CFP-Chore-Key`` marker and
    the legacy ``candidate false positive`` title marker) are deduplicated by
    item id. A failed lookup contributes nothing rather than aborting the
    sweep.
    """
    run = wl_run or _run_wl
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    queries = (
        ["wl", "list", "--tags", REFACTOR_TAG, "--json"],
        ["wl", "list", "CFP-Chore-Key:", "--json"],
        ["wl", "list", AUDIT_FP_TITLE_MARKER, "--json"],
    )
    for query in queries:
        for item in _items_from(run(query, worklog_dir)):
            item_id = str(item.get("id") or "")
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            items.append(item)
    return items


# ---------------------------------------------------------------------------
# Evidence + closure
# ---------------------------------------------------------------------------


def build_evidence_comment(result: Classification) -> str:
    """Build the evidence comment recorded before closing an item."""
    stamped = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"{EVIDENCE_MARKER} {result.classification} — {result.reason}",
        f"- **Family:** {result.family}",
        f"- **Finding:** `{result.code or '?'}` in `{result.path or '?'}`",
    ]
    if result.command:
        lines.append(f"- **Command:** `{result.command}`")
    lines.append(f"- **Hits:** {result.hit_count}")
    if result.recorded_fingerprint:
        lines.append(f"- **Recorded fingerprint:** `{result.recorded_fingerprint}`")
    if result.current_fingerprint:
        lines.append(f"- **Current fingerprint:** `{result.current_fingerprint}`")
    if result.recorded_fingerprint and result.current_fingerprint:
        changed = result.recorded_fingerprint != result.current_fingerprint
        lines.append(f"- **Target changed since filing:** {'yes' if changed else 'no'}")
    lines.append(f"- **Verified:** {stamped}")
    return "\n".join(lines)


def build_close_reason(result: Classification) -> str:
    """Build the recorded close rationale for an actionable item."""
    if result.classification == CLASS_GONE:
        return (
            f"reverify: target file no longer exists ({result.path}) — the "
            "finding can never reproduce"
        )
    return (
        f"reverify: {result.code or 'finding'} no longer reproduces in "
        f"{result.path} (0 findings from `{result.command}`)"
    )


def _apply_one(
    result: Classification,
    wl_run: Callable[[Sequence[str], str | None], dict[str, Any] | None],
    worklog_dir: str | None,
) -> dict[str, Any]:
    """Comment + close one actionable item. A failed comment blocks the close."""
    comment = build_evidence_comment(result)
    commented = wl_run(
        [
            "wl", "comment", "add", result.item_id,
            "--comment", comment,
            "--author", REVERIFY_AUTHOR,
            "--json",
        ],
        worklog_dir,
    )
    if commented is None or commented.get("success") is False:
        return {
            "id": result.item_id,
            "action": "error",
            "stage": "comment",
            "error": "wl comment add failed",
        }
    reason = build_close_reason(result)
    closed = wl_run(
        [
            "wl", "close", result.item_id,
            "--reason", reason,
            "--author", REVERIFY_AUTHOR,
            "--json",
        ],
        worklog_dir,
    )
    if closed is None:
        return {
            "id": result.item_id,
            "action": "error",
            "stage": "close",
            "error": "wl close failed",
        }
    if closed.get("failed") or closed.get("success") is False:
        return {
            "id": result.item_id,
            "action": "error",
            "stage": "close",
            "error": f"wl close reported failure: {json.dumps(closed)[:200]}",
        }
    return {
        "id": result.item_id,
        "action": "closed",
        "classification": result.classification,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Sweep orchestration
# ---------------------------------------------------------------------------


def run_sweep(
    items: Sequence[dict[str, Any]],
    repo_root: str,
    *,
    apply: bool = False,
    run_command: Callable[[Sequence[str], str], subprocess.CompletedProcess]
    | None = None,
    wl_run: Callable[[Sequence[str], str | None], dict[str, Any] | None]
    | None = None,
    worklog_dir: str | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    """Classify *items* and (optionally) close the stale ones.

    This is the single entry point both the CLI and the tests drive. It is
    pure with respect to the worklog unless ``apply`` is true.
    """
    run_wl = wl_run or _run_wl
    results: list[Classification] = []
    for item in items:
        try:
            result = classify_item(
                item, repo_root, run_command=run_command, timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001 -- one bad item never aborts a sweep
            is_dict = isinstance(item, dict)
            result = Classification(
                item_id=str(item.get("id") or "") if is_dict else "",
                title=str(item.get("title") or "") if is_dict else "",
                family="unknown",
                reason=f"classification error: {exc}",
            )
        if result is not None:
            results.append(result)

    counts = {name: 0 for name in CLASSIFICATIONS}
    for result in results:
        counts[result.classification] = counts.get(result.classification, 0) + 1

    applied: list[dict[str, Any]] = []
    if apply:
        for result in results:
            if result.classification in ACTIONABLE_CLASSIFICATIONS:
                applied.append(
                    _apply_one(result, run_wl, worklog_dir)
                )

    return {
        "dry_run": not apply,
        "repo_root": repo_root,
        "scanned": len(items),
        "attributed": len(results),
        "counts": counts,
        "items": [result.to_dict() for result in results],
        "applied": applied,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments (dry-run is the default)."""
    parser = argparse.ArgumentParser(
        description=(
            "Re-verify linter-derived work items against the current tree. "
            "Dry-run by default; pass --apply to close stale findings."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Close items classified target-gone or no-longer-reproduces "
            "(with an evidence comment). Without this flag the sweep only "
            "reports and makes no worklog changes."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help=(
            "Repository root to resolve finding paths against. Defaults to "
            "the main checkout (shared git directory's parent)."
        ),
    )
    parser.add_argument(
        "--worklog-dir",
        default=None,
        help="Explicit .worklog directory (bypassed directory resolution).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Per-command timeout in seconds (default: 120).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging.",
    )
    return parser.parse_args(argv)


def _render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    mode = "DRY RUN" if report["dry_run"] else "APPLY"
    lines.append(f"Re-verification sweep ({mode})")
    lines.append(f"Repository: {report['repo_root']}")
    lines.append(
        f"Scanned {report['scanned']} candidate item(s); "
        f"{report['attributed']} attributable to a linter family."
    )
    counts = report["counts"]
    lines.append(
        "  "
        + ", ".join(
            f"{name}={counts.get(name, 0)}" for name in CLASSIFICATIONS
        )
    )
    actionable = [
        item for item in report["items"]
        if item["classification"] in ACTIONABLE_CLASSIFICATIONS
    ]
    if actionable:
        lines.append("")
        lines.append("Stale items:")
        for item in actionable:
            lines.append(
                f"  {item['id']} [{item['classification']}] {item['title'][:90]}"
            )
    unverifiable = [
        item for item in report["items"]
        if item["classification"] == CLASS_UNVERIFIABLE
    ]
    if unverifiable:
        lines.append("")
        lines.append("Unverifiable (left untouched):")
        for item in unverifiable:
            lines.append(f"  {item['id']}: {item['reason']}")
    if report["applied"]:
        lines.append("")
        lines.append("Applied:")
        for outcome in report["applied"]:
            lines.append(
                f"  {outcome.get('id')}: {outcome.get('action')}"
                + (
                    f" ({outcome.get('error')})"
                    if outcome.get("action") == "error"
                    else ""
                )
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    repo_root = resolve_repo_root(args.repo_root)
    items = load_candidates(worklog_dir=args.worklog_dir)
    report = run_sweep(
        items,
        repo_root,
        apply=args.apply,
        worklog_dir=args.worklog_dir,
        timeout=args.timeout,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(_render_text(report))

    errors = [item for item in report["applied"] if item.get("action") == "error"]
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
