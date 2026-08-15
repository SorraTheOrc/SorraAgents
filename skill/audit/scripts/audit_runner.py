#!/usr/bin/env python3
"""Audit runner – deterministic audit orchestration.

Provides two subcommands:
  issue <id>   – audit a single work item
  project      – audit the overall project

Usage:
  audit_runner.py issue <id> [--do-not-persist] [--pi-bin pi] [--model <name>] [--run-tests]
  audit_runner.py project [--pi-bin pi] [--model <name>]

Verdicts:
  met       – acceptance criterion fully satisfied
  unmet     – acceptance criterion not satisfied
  partial   – acceptance criterion partially satisfied
  adjusted  – acceptance criterion adapted with acceptable variance
              (does not block ready-to-close, recorded in variance decisions)

Execution-dependent criteria (e.g. 'full project test suite passes'):
  by default the runner never executes the suite (read-only mandate).
  Evidence is supplied either by the operator-attested ``--green-run`` path
  or, automatically, by a green full-suite run found READ-ONLY in the
  per-repo test cache (query_cached — see SA-0MSIU5HFI0024D7W). With the
  explicit ``--run-tests`` flag (SA-0MSJELSWS002UF60) the runner instead
  invokes the test skill (run_tests.py) to execute the full suite when the
  cache is missing/stale, triaging failures per the test skill. All paths
  are fail-closed: missing/mismatched evidence leaves such ACs partial.

Persist + verify invariant:
  Unless ``--do-not-persist`` is given, the runner ALWAYS persists the
  audit report via ``persist_audit()`` and then performs a readback
  verification via ``wl audit-show --json`` to confirm the stored audit
  is retrievable.  If either step fails the runner exits non-zero.
  This is not configurable — it is an invariant of the runner.

Exit codes:
  0 – success (report printed to stdout)
  1 – Worklog / CLI / Pi failure, persistence failure, or readback
      verification failure
  2 – argument error
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import shlex
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.audit.scripts.persist_audit import (
    PERSIST_CONTENT_INVALID,
    persist_audit,
)
from skill.scripts.failure_notice import FailureNotice
from skill.scripts.pi_utils import extract_pi_text
from skill.shared.process_semaphore import (
    DEFAULT_MAX_WORKERS,
    ENV_MAX_WORKERS,
    Semaphore,
)
from skill.shared.status_lifecycle import (
    SIBLING_SCAN_ROOT as SHARED_SIBLING_SCAN_ROOT,
)
from skill.shared.status_lifecycle import (
    _extract_work_item_prefix as _extract_work_item_prefix_shared,
)
from skill.shared.status_lifecycle import (
    _find_worklog_dir_by_prefix as _find_worklog_dir_by_prefix_shared,
)
from skill.shared.status_lifecycle import (
    _wl_error_detail,
)
from skill.shared.status_lifecycle import (
    resolve_worklog_flags as shared_resolve_worklog_flags,
)
from skill.test.scripts.run_tests import (
    full_suite_commands,
    parse_node_failures,
    parse_pytest_failures,
    pytest_command,
)
from skill.test_cache import DEFAULT_TTL_SECONDS, query_cached, run_cached

# ---------------------------------------------------------------------------
# Concurrency control (fan-out bounding, SA-0MSAEKOQE009TEB4)
# ---------------------------------------------------------------------------
AUDIT_SEMAPHORE_NAME = "audit"
AUDIT_LOCK_TIMEOUT_ENV = "AUDIT_LOCK_TIMEOUT"
AUDIT_LOCK_TIMEOUT_DEFAULT = 0.0
"""Fail-fast wait (seconds) for a free audit concurrency slot.

Default is 0s: when the ceiling is saturated, the pi call fails
immediately with an ``unmet`` verdict ("Audit concurrency limit reached")
instead of blocking. The parent bash-tool execution timeout (~120s) is
shorter than any long bounded wait, so a long default wait previously
killed audits mid-wait. Operators can opt into a bounded wait via the
``AUDIT_LOCK_TIMEOUT`` environment variable.
"""

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CHILDREN_CAP = 10

CALL_PI_TIMEOUT = 1800
"""Internal timeout (seconds) for each Pi model subprocess call.

This is a generous safety net for individual Pi model calls during audit
processing. Phase 2 deep analysis now runs Pi in agent mode (with file-reading
tools), which is inherently slower than bare LLM calls. 1800s (30 min)
accommodates typical agent-mode analyses where the model reads and searches
multiple implementation files.

The cumulative elapsed-time guard in ``cmd_issue`` (default
``PARENT_TIMEOUT_DEFAULT + N x PARENT_TIMEOUT_PER_CHILD``, scaled by the
number of active children — see ``_default_parent_timeout``) provides the
primary protection against the parent bash-tool execution timeout (~120s),
not this per-call timeout. The guard can be overridden via the
``--parent-timeout`` CLI flag or the ``AUDIT_PARENT_TIMEOUT`` environment
variable (see ``_resolve_parent_timeout``) for harnesses whose bash tool
allows longer runs.

If the Pi model itself takes longer than this value, something is likely
wrong (model hang, provider issue) and the timeout diagnostic should be
produced rather than blocking indefinitely.

The effective per-call timeout can be raised via the ``--timeout`` CLI flag
or the ``AUDIT_PI_TIMEOUT`` environment variable (see
``_resolve_effective_timeout``).
"""

PARENT_TIMEOUT_DEFAULT = 110
"""Base term (seconds) of the default cumulative elapsed-time guard in
``cmd_issue``.

The effective default guard scales with the number of active children:
``PARENT_TIMEOUT_DEFAULT + N x PARENT_TIMEOUT_PER_CHILD`` (see
``_default_parent_timeout``). A single-child parent gets a ~710s budget and
a 10-child parent gets ~6,110s, so multi-child audits with default settings
attempt child auto-audits instead of silently degrading to parent-only.

When the total elapsed time since the audit start marker exceeds the guard,
remaining child audits are skipped with a clear diagnostic instead of risking
a silent kill by the parent bash-tool execution timeout (~120s).

The guard can be overridden with an exact value via the ``--parent-timeout``
CLI flag or the ``AUDIT_PARENT_TIMEOUT`` environment variable (see
``_resolve_parent_timeout``) for harnesses that allow longer parent runs.
"""

PARENT_TIMEOUT_PER_CHILD = 600
"""Per-child budget (seconds) added to ``PARENT_TIMEOUT_DEFAULT`` when the
default elapsed-time guard is scaled by the number of active children (see
``_default_parent_timeout``). ~10 min per child accommodates typical child
audit wall times without letting a pathological parent run hang indefinitely.
"""

AUDIT_PI_TIMEOUT_ENV = "AUDIT_PI_TIMEOUT"
"""Environment variable name for overriding the per-call Pi timeout.

When set (and ``--timeout`` is not passed), this value (seconds) is used as
the effective per-call Pi model timeout instead of ``CALL_PI_TIMEOUT``.
This lets release operators raise the audit runner's tolerance for slow
Pi model calls (e.g. ``AUDIT_PI_TIMEOUT=3600``) without changing defaults.
"""

AUDIT_CHILD_SCREEN_TIMEOUT_ENV = "AUDIT_CHILD_SCREEN_TIMEOUT"
"""Environment variable name for the child Phase-1 AC-review screen budget.

Child Phase-1 AC-review screens are lightweight (30-190 s healthy) so they
use a short per-call budget (``_CHILD_SCREEN_TIMEOUT_DEFAULT`` = 600 s)
instead of the full 1800 s Phase-2 budget. A screen that exceeds this
budget returns a clean timeout verdict (existing ``_timeout`` marker +
timeout evidence) — never a full 1800 s burn. Overridable via the
``--child-screen-timeout`` CLI flag (flag wins) or this env var.
"""

_CHILD_SCREEN_TIMEOUT_DEFAULT = 600
"""Default per-call budget (seconds) for child Phase-1 AC-review screens.

Set conservatively above healthy child Phase-1 durations (30-190 s) but far
below the 1800 s Phase-2 budget, so a stalled lightweight child screen fails
fast instead of burning a full 30-min budget (LP-0MSQ32S2M001EA74 AC1).
"""

AUDIT_STALL_TIMEOUT_ENV = "AUDIT_STALL_TIMEOUT"
"""Environment variable name for the in-process stall-abort threshold.

Any single Pi call (Phase 1 or Phase 2) that produces no output/progress
for at least this many seconds is aborted in-process inside ``_call_pi``
(``proc.kill()`` + drain) instead of waiting out the remaining per-call
budget. Default ``_STALL_TIMEOUT_DEFAULT`` = 600 s (10 min); invalid values
are ignored with a warning. The external monitored-run stale-log abort
(>= 10 min, SKILL.md) remains as a backstop.
"""

_STALL_TIMEOUT_DEFAULT = 600
"""Default in-process stall-abort threshold (seconds).

10 min is generous vs healthy Phase 1 screens (30-190 s) while still aborting
a genuinely hung call well before its 1800 s budget (LP-0MSQ32S2M001EA74 AC2).
"""


FP_SCREEN_CONTEXT = "false-positive-screen"
"""Debug-log context label for the model-judged false-positive screen.

The screen classifies ruff code-quality findings via a single batched Pi
call (SA-0MST01NPD007MYG4 / SA-0MST01O4G002VPBR). Tests assert on this
context to prove the screen is (not) invoked.
"""

FP_SCREEN_VALID_CLASSIFICATIONS = frozenset(
    {"genuine", "confident-false-positive", "uncertain"}
)
"""Valid per-finding screen classifications.

``uncertain`` is the caution-first default: a finding that is missing from
the batch response, unparseable, or degraded by a provider failure is never
classified ``confident-false-positive`` (T1 AC1/AC2).
"""

FP_CANDIDATE_ANNOTATION = "candidate false positive — producer decision required"
"""Annotation appended to uncertain-screen findings that still block.

An ``uncertain`` classification never triggers remediation; the finding
remains blocking under ``_has_phase1_blocking_issues`` and is annotated so
a producer makes the final call (SA-0MST01O4G002VPBR AC4).
"""

FP_SCREEN_FAILED_JUSTIFICATION = (
    "Screen output could not be parsed or the Pi call failed — all findings "
    "defaulted to uncertain (caution-first)."
)
"""Justification recorded when the whole screen degrades (T1 AC2)."""

FP_CHORE_ANNOTATION = FP_CANDIDATE_ANNOTATION
"""Annotation for medium/low confident-false-positive tracking chores.

The chore links the finding for a producer decision but carries NO commit
link (no config change happened — SA-0MST01PQQ009T0CI AC2).
"""

FP_SCREEN_MISSING_JUSTIFICATION = (
    "Finding missing from the screen response — defaulted to uncertain "
    "(caution-first)."
)
"""Justification recorded for a finding absent from the batch response.

Never ``confident-false-positive``: a finding the model did not see cannot
be declared a confident false positive (T1 AC1).
"""


AUDIT_REMEDIATION_MAX_ITERATIONS_ENV = "AUDIT_REMEDIATION_MAX_ITERATIONS"
"""Environment variable name for the config-fix iteration cap.

The remediation loop (T2/F2) applies at most this many minimal ruff config
edits per audit run. Default ``REMEDIATION_MAX_ITERATIONS_DEFAULT`` = 3;
invalid values fail closed to the default.
"""

REMEDIATION_MAX_ITERATIONS_DEFAULT = 3
"""Default cap on config-fix iterations per audit run (T2 AC5)."""

REMEDIATION_EXHAUSTED_ANNOTATION = "remediation loop exhausted"
"""Annotation for a finding still present after the iteration cap.

A confident-false-positive finding that persists after ``max_iterations``
config-fix iterations is demoted to blocking ``genuine`` with this
annotation (T2 AC5) — the audit never suppresses it silently.
"""

REMEDIATION_COMMIT_MESSAGE = "audit: remediate ruff false positives (per-file-ignores)"
"""Local (no-push) commit message for each applied config fix (T2 AC3)."""


AUDIT_GREEN_RUN_ENV = "AUDIT_GREEN_RUN"
"""Environment variable name for the operator-attested green test run.

When set (and ``--green-run`` is not passed), this value (an exact commit
sha or the alias ``HEAD``) is used as the green-run attestation. Precedence:
``--green-run`` flag > ``AUDIT_GREEN_RUN`` env var > unset (no attestation).
"""

AUTO_GREEN_RUN_BLOCK_HEADER = "AUTO-VERIFIED GREEN RUN"
"""Header of the automatic full-suite verification block injected into prompts.

The automatic path (SA-0MSIU5HFI0024D7W) consumes a green full-suite run
from the per-repo test cache (``query_cached``) READ-ONLY — the runner never
executes the suite. The block tells the model that execution-dependent
criteria (e.g. 'full test suite passes') MAY be marked met based on that
verified cached result, while the read-only mandate otherwise remains in force.
"""

TEST_SKILL_RUN_BLOCK_HEADER = "TEST-SKILL GREEN RUN"
"""Header of the executed full-suite verification block injected into prompts.

The ``--run-tests`` path (SA-0MSJELSWS002UF60) executes the full project
test suite via the test skill's runner (``run_tests.py``) as an explicit,
operator-authorized deviation from the audit's read-only mandate, then
injects this block so the model MAY mark execution-dependent criteria met
based on the executed green run. Distinct from ``AUTO_GREEN_RUN_BLOCK_HEADER``
(read-only cache consumption) and ``GREEN-RUN ATTESTATION`` (operator
attestation).
"""

AUDIT_TEST_SKILL_RUN_TIMEOUT = 600
"""Per-command timeout (seconds) for the test-skill invocation (``--run-tests``).

The audit runner delegates full-suite execution to the test skill's runner
machinery (``skill/test/scripts/run_tests.py``) when the operator passes
``--run-tests`` and no cached green evidence exists. Each suite command
(pytest + node suite dirs) is executed with this timeout, matching the
default used by ``run_tests.py`` itself.
"""

AUDIT_PARENT_TIMEOUT_ENV = "AUDIT_PARENT_TIMEOUT"
"""Environment variable name for overriding the cumulative elapsed-time guard.

When set (and ``--parent-timeout`` is not passed), this value (seconds)
replaces the scaled default (``_default_parent_timeout``) as the elapsed-time
threshold for skipping remaining child audits in ``cmd_issue``. This lets
release operators extend the overall audit run budget (e.g.
``AUDIT_PARENT_TIMEOUT=3600``) without changing defaults.
"""

AUDIT_MAX_CHILD_AUDITS_ENV = "AUDIT_MAX_CHILD_AUDITS"
"""Environment variable bounding the per-run recursive child-audit cascade.

When set (a positive integer) and ``--max-child-audits`` is not passed, this
is the maximum number of child audits that a single parent ``cmd_issue`` run
may auto-trigger (SA-0MSKB6V5Q007YDHE). An invalid value is ignored with a
warning and the default cap is used.
"""

_DEFAULT_MAX_CHILD_AUDITS = 5
"""Default per-run cap on auto-triggered recursive child audits.

Bounds the wall-clock of the child cascade even when the operator explicitly
opts in with ``--audit-children``: a parent with many unaudited children can
no longer silently spawn an unbounded number of child audit subprocesses
(SA-0MSKB6V5Q007YDHE).
"""

_DEFAULT_MAX_CITATIONS_PER_AC = 5
"""Default cap on file:line evidence citations per AC in Phase 2 deep prompts.

Phase 2 deep analysis is 66% of audit model time; the dominant cost is long
evidence-JSON generation on the local model, not context size. Bounding the
number of file:line references the model may cite per criterion (default 5,
minimum 1) shortens evidence generation without changing the model or
verdict semantics (LP-0MSQ32WM5000NCB7 AC1). Prompt-level only: parsed
evidence/verdicts are never mutated.
"""

_PI_MAX_RETRIES = 2
"""Number of retries for transient provider errors in ``_call_pi``.

Providers sometimes terminate a model call with ``finish_reason: error``
(e.g., local proxy glitches) before the model emits its final output. The
audit runner treats these as transient and retries up to this many extra
times before surfacing a provider-error diagnostic. Non-provider failures
(e.g., unparseable output, timeouts) are NOT retried.
"""

_PI_RETRY_BACKOFF_SECONDS = 2.0
"""Base backoff (seconds) between provider-error retries in ``_call_pi``.

Backoff grows linearly: 1x, 2x, ... base per retry attempt.
"""

_STATUS_RESTORE_MAX_ATTEMPTS = 3
"""Total attempts (1 initial + retries) for the terminal status restore.

The verdict-driven status transition in ``cmd_issue``'s ``finally`` block is
retried on transient ``wl`` failures so a single hiccup never leaves the work
item stuck ``in_progress`` (which breaks the release close step — see
SA-0MSAL2NQV0008HY5). The update is idempotent, so retrying after a partially
applied update is harmless.
"""

_STATUS_RESTORE_RETRY_DELAY_S = 0.5
"""Base delay (seconds) between status-restore retries; grows linearly."""

_SCANNING_BLOCK = (
    "SCANNING — When you need to look something up, use the bounded helpers:\n"
    "- Worklog lookups: `wl search <keywords> --json` or `wl list <term> --json` for substring matching, `scan.py find-workitem <id>` for exact match (never `grep -r` over .worklog/).\n"
    "- Code search: `python3 skill/audit/scripts/scan.py search-code <pattern> --path <dir> --type py` (bounded rg with prunes).\n"
    "- File listing: `python3 skill/audit/scripts/scan.py list-files --path <dir> --type py`.\n"
    "- NEVER run unbounded recursive grep over the repo root or .worklog/ (e.g. `grep -r ... .` or `grep -r ... .worklog/`).\n\n"
)
"""Canonical bounded-scanning guidance injected into every audit prompt.

The single SCANNING block reused by ALL phases (SA-0MSL1Z6M7002EDS0):
Phase 1 parent + child AC reviews and Phase 2 parent deep / child deep /
batch prompts all interpolate this one constant so the guidance can never
drift between sites (previously three inline copies had already diverged).
It is the fuller version with the ``list-files`` bullet and the
single-file ``.worklog`` grep note; the model reads only in-scope files and
uses the bounded ``scan.py`` helpers instead of unbounded repository
exploration (P7 / Phase 2 performance pattern).
"""

_PHASE2_MAX_RETRIES = 1
"""Provider-error retry cap for long agent-mode Phase 2 calls.

Phase 2 deep analysis (``phase2_deep`` / ``phase2_child``) runs Pi in agent
mode with file-reading tools, so each call is long. A provider error late in
such a call must not restart the entire call multiple times (worst case
~3 x 1800s before this change); retrying at most once bounds the cost while
still giving transient provider glitches a chance to recover. Short Phase 1
bare calls keep ``_PI_MAX_RETRIES`` (2).
"""

AUDIT_PARALLELISM_ENV = "AUDIT_PARALLELISM"
"""Environment variable controlling child deep-analysis concurrency.

When set (an integer >= 1), this is the maximum number of independent child
``phase2_child:<i>`` Pi calls that may run concurrently inside
``_run_phase2_deep_analysis``. The parent deep-analysis call always runs
first and is never parallelized. Setting this to 1 restores the historical
strictly-sequential behavior. Invalid values are ignored with a warning.
"""

AUDIT_PHASE2_PARALLELISM_ENV_LEGACY = "AUDIT_PHASE2_PARALLELISM"
"""Legacy name for ``AUDIT_PARALLELISM_ENV`` — kept for backward compatibility.

When ``AUDIT_PARALLELISM`` is not set, the runner falls back to this legacy
name so existing scripts and automation are not broken."""

_PARALLELISM_DEFAULT = 2
"""Default bounded concurrency cap for child deep-analysis calls.

Chosen conservatively so the local model proxy is not overwhelmed while
still collapsing the N-sequential-calls wall-clock to N/cap. Operators can
override via ``AUDIT_PARALLELISM``.
"""

AUDIT_PROXY_BASE_URL_ENV = "AUDIT_PROXY_BASE_URL"
"""Environment variable name for the llm-manager proxy base URL.

The runner queries ``GET <base>/admin/mode`` at start to detect the proxy's
operating mode (fast/cheap) and serialize parallelism when cheap
(SA-0MSN04X2S006ONH0). Defaults to ``AUDIT_PROXY_BASE_URL_DEFAULT``.
"""

AUDIT_PROXY_BASE_URL_DEFAULT = "http://192.168.0.199:8000"
"""Default llm-manager proxy base URL (matches pi ``models.json`` "Local Proxy").

Overridable via ``AUDIT_PROXY_BASE_URL`` if the proxy address changes — no
code change required.
"""

AUDIT_PROXY_MODE_TIMEOUT = 3.0
"""Short timeout (seconds) for the proxy-mode query (fail-open).

The mode query is best-effort: a timeout/unreachable proxy must never block
or fail the audit, so the wait is capped at ~3 s.
"""

AUDIT_SLOT_STATUS_URL_ENV = "AUDIT_SLOT_STATUS_URL"
"""Environment variable name for the local proxy slot-status endpoint.

The runner queries this endpoint to derive the dynamic child-call
concurrency ceiling (LP-0MSQ32S2M001EA74 AC3). Defaults to
``AUDIT_SLOT_STATUS_URL_DEFAULT`` (http://localhost:8000/llama/local/status).
"""

AUDIT_SLOT_STATUS_URL_DEFAULT = "http://localhost:8000/llama/local/status"
"""Default local proxy slot-status endpoint (``/llama/local/status``).

Reports ``available_slots``/``total_slots`` from llama-server ``/slots`` with
fail-open to ``session_slot_pool_size`` when no model is loaded
(LP-0MSI06HPB0043MV1).
"""

AUDIT_SLOT_STATUS_TIMEOUT = 1.0
"""Short timeout (seconds) for the slot-status query (fail-open).

The dynamic ceiling must never block or fail the audit when the endpoint
is unavailable — a 1 s timeout keeps the best-effort query cheap.
"""

AUDIT_MAX_CHILD_CONCURRENCY_ENV = "AUDIT_MAX_CHILD_CONCURRENCY"
"""Environment variable name for the max child-call concurrency cap.

Caps the dynamic slot-aware ceiling (LP-0MSQ32S2M001EA74 AC3). Defaults to
``_resolve_parallelism()`` (``AUDIT_PARALLELISM`` env or 2),
preserving the existing static knob as the floor/fallback.
"""


AUDIT_FRESHNESS_BUFFER_SECONDS = 60
"""Freshness buffer (seconds) for the recent-audit gate.

When the audit's ``auditedAt`` timestamp is more recent than the work item's
``updatedAt`` timestamp plus this buffer, the audit is considered fresh and
the runner skips the full audit pipeline. This time gate remains as a floor
for audits that carry no content fingerprint (SA-0MSKB6US1009CNHT); audits
with a fingerprint are gated on content match instead (see
``_check_audit_freshness``).
"""

AUDIT_CONTENT_FINGERPRINT_PREFIX = "Audit content fingerprint: "
"""Prefix of the content-fingerprint metadata line embedded in audit reports.

The content fingerprint (git HEAD sha + work-item description hash + Key Files
list, captured at audit time) is embedded in the persisted report so a re-audit
of an unchanged item can skip the pipeline in seconds instead of re-running it
(SA-0MSKB6US1009CNHT). The line is parsed back out by
``_extract_content_fingerprint``.
"""

AUDIT_PERSIST_WRITE_TOLERANCE_SECONDS = 30
"""Tolerance (seconds) for treating an audit as fresh despite a stale check.

Persisting an audit bumps the work item's ``updatedAt`` (``wl audit-set`` +
``wl update --audit-text`` + the verdict-driven status transition), so
``auditedAt`` is always a fraction of a second to a few seconds BEFORE the
final ``updatedAt``. The plain freshness gate (``auditedAt > updatedAt +
buffer``) can therefore never hold for a just-persisted audit, which made the
parent runner re-trigger child audits forever. When the child's ``updatedAt``
is at-or-slightly-after the audit's ``auditedAt`` within this tolerance, the
update is the audit's own persistence write and the audit is trusted instead
of being flagged stale (SA-0MSI3XH34001LLU4).
"""

# Verdict constants
VERDICT_MET = "met"
VERDICT_UNMET = "unmet"
VERDICT_PARTIAL = "partial"
VERDICT_ADJUSTED = "adjusted"
_ACCEPTABLE_VERDICTS = {VERDICT_MET, VERDICT_ADJUSTED}


def _normalize_verdict(verdict: str | None) -> str:
    """Normalize a model-provided verdict to the runner vocabulary.

    The audit prompts request verdicts from {met, unmet, partial, adjusted},
    but models occasionally use synonyms (e.g. "pass" for "met"). Without
    normalization a satisfied criterion blocks closure
    (SA-0MSDOU2SV006J91X). Known synonyms are mapped to the canonical
    verdict; unknown values pass through unchanged so the caller's strict
    acceptable-verdict check still flags them for review.
    """
    v = (verdict or "").strip().lower()
    synonyms = {
        "pass": VERDICT_MET,
        "passed": VERDICT_MET,
        "ok": VERDICT_MET,
        "satisfied": VERDICT_MET,
        "fail": VERDICT_UNMET,
        "failed": VERDICT_UNMET,
        "not met": VERDICT_UNMET,
        "not-met": VERDICT_UNMET,
    }
    return synonyms.get(v, v)

# ---------------------------------------------------------------------------
# Closing-sentence constants (AC1–3)
# ---------------------------------------------------------------------------
_CLOSING_READY = (
    "Audit passed. The item is ready for release."
)
_CLOSING_NOT_READY = (
    "Work item is not ready to close (see above), "
    "would you like me to address the gaps in the audit?"
)

# Model / config constants (following Ralph's pattern)
ASSET_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "ralph" / "assets" / ".ralph.json"
DEFAULT_MODEL = "Local Proxy/plan"
DEFAULT_MODEL_SOURCE = "local"
MODEL_SOURCES = frozenset({"remote", "local"})
RALPH_CONFIG_FILES = [
    Path(".ralph.json"),
    Path("ralph.config.json"),
]
AUDIT_PHASE = "audit"

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_closing_sentence(report: str) -> str:
    """Determine the closing sentence based on the ready-to-close verdict.

    Parses the first ``Ready to close:`` line in *report* and returns the
    appropriate closing sentence. Defaults to *not ready* when the line is
    not found or the verdict is not ``Yes``.

    This function also handles reports that have been wrapped by a
    ``FailureNotice`` (where the first line is ``═══`` rather than
    ``Ready to close:``).
    """
    for line in report.splitlines():
        stripped = line.strip()
        if stripped.startswith("Ready to close:"):
            verdict = stripped.split(":", 1)[1].strip()
            if verdict.lower() == "yes":
                return _CLOSING_READY
            break
    return _CLOSING_NOT_READY


def _parse_ready_to_close(report: str) -> str | None:
    """Parse the ``Ready to close:`` verdict from an audit report.

    Returns ``"yes"`` or ``"no"`` when a verdict line is found, ``None``
    when the report has no parseable verdict (failure/unparseable output).
    Also handles reports wrapped by a ``FailureNotice`` (where the first
    line is ``═══`` rather than ``Ready to close:``).
    """
    for line in report.splitlines():
        stripped = line.strip()
        if stripped.startswith("Ready to close:"):
            verdict = stripped.split(":", 1)[1].strip()
            if verdict.lower() == "yes":
                return "yes"
            return "no"
    return None


# Diagnostic evidence strings produced by the audit runner's infrastructure-
# failure fallback blocks (SA-0MSG9SLGI002OF7V). When any AC verdict carries
# one of these markers, the audit verdict cannot be trusted as an explicit
# model assessment — the run degraded to diagnostic 'partial' fallbacks.
_INFRA_FALLBACK_MARKERS = (
    "Pi model output could not be parsed",
    "Pi provider error",
    "Audit concurrency limit reached",
    "timed out \u2014 manual review required",
)


def _evidence_has_infra_failure_markers(ac_results: list[dict],
                                        child_results: list[dict]) -> bool:
    """Return True when any parent/child AC evidence carries an infra-failure marker.

    Backstop for the ``ac_fallback_used`` flag: if a future fallback site
    forgets to set the flag, the diagnostic evidence it writes still carries
    a recognizable marker (e.g. ``Pi model output could not be parsed``,
    ``Pi provider error``, ``Audit concurrency limit reached``, ``timed out
    \u2014 manual review required``), so a "No" verdict produced solely from
    infrastructure failure is still restored rather than demoted.
    """
    for r in ac_results:
        evidence = r.get("evidence", "") or ""
        if any(marker in evidence for marker in _INFRA_FALLBACK_MARKERS):
            return True
    for cr in child_results:
        for r in cr.get("ac_results", []):
            evidence = r.get("evidence", "") or ""
            if any(marker in evidence for marker in _INFRA_FALLBACK_MARKERS):
                return True
    return False


def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def _cwd_aware_runner(git_root: Path,
                      base_runner: Runner | None = None) -> Runner:
    """Wrap *base_runner* so git commands resolve against *git_root*.

    When *git_root* equals the launch cwd's project root
    (``TARGET_PROJECT_ROOT``) — an owning-project or worktree launch — the
    base runner is returned unchanged (commands pass through byte-identical;
    zero regression for the standard path). When they differ (a non-owning
    launch whose ownership was resolved from the worklog), every ``git``
    command gets ``git -C <git_root>`` prepended so file-scope manifests,
    HEAD shas, working-tree hashes, and green-run evidence resolve against
    the owning project's repository — never the launch cwd's
    (SA-0MSLLGDW00098UCC).
    """
    base = base_runner if base_runner is not None else _default_runner
    launch_root = TARGET_PROJECT_ROOT.resolve()
    git_root = git_root.resolve()
    if git_root == launch_root:
        return base

    def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
        if cmd and cmd[0] == "git":
            cmd = ["git", "-C", str(git_root)] + list(cmd[1:])
        return base(cmd)

    return _run


def _extract_work_item_prefix(cmd: Sequence[str]) -> str | None:
    """Extract the work-item id prefix (e.g. ``OSL``) from a wl command.

    Thin wrapper over the shared implementation in
    ``skill.shared.status_lifecycle`` (SA-0MSG57UNY009DE51).
    """
    return _extract_work_item_prefix_shared(cmd)


def _find_worklog_dir_by_prefix(prefix: str) -> Path | None:
    """Find the ``.worklog`` dir of a sibling project with matching config prefix.

    Thin wrapper over the shared implementation in
    ``skill.shared.status_lifecycle`` (SA-0MSG57UNY009DE51).
    """
    return _find_worklog_dir_by_prefix_shared(prefix)


def _resolve_worklog_flags(cmd: Sequence[str],
                           explicit_dir: str | None = None) -> list[str]:
    """Resolve ``--worklog-dir`` flags for a wl command.

    Thin wrapper over the shared :func:`resolve_worklog_flags` in
    ``skill.shared.status_lifecycle`` (SA-0MSG57UNY009DE51). Resolution
    order (unchanged): explicit dir > prefix-to-sibling scan > cwd-chain
    fallback > no flag.
    """
    return shared_resolve_worklog_flags(cmd, explicit_dir=explicit_dir)


def _run_wl(runner: Runner, cmd: Sequence[str],
            worklog_dir: str | None = None) -> dict:
    """Run a ``wl`` command via the injectable *runner* and return parsed JSON.

    Injects ``--worklog-dir`` flags (resolved per :func:`_resolve_worklog_flags`)
    so the command targets the correct worklog store regardless of the caller's
    cwd. ``worklog_dir`` is an explicit override (highest precedence).
    """
    full_cmd = list(cmd)
    if full_cmd and full_cmd[0] == "wl":
        full_cmd[1:1] = _resolve_worklog_flags(full_cmd, explicit_dir=worklog_dir)
    proc = runner(full_cmd)
    if proc.returncode != 0:
        raise RuntimeError(
            f"wl command failed ({' '.join(full_cmd)}): {_wl_error_detail(proc)}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from wl: {exc}") from exc
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(
            f"Worklog command failed: {data.get('error', 'unknown error')}"
        )
    return data


def _run_wl_projected(runner: Runner, cmd: Sequence[str], jq_expr: str,
                      worklog_dir: str | None = None):
    """Run a ``wl`` command piped through ``jq`` and return the projection.

    Injects ``--worklog-dir`` flags exactly like :func:`_run_wl`, then pipes
    the full output through ``jq <jq_expr>``. The OS pipe between ``wl`` and
    ``jq`` is unbounded, so only the small projection crosses into the
    process buffer — large dumps (e.g. ``wl list --status completed`` at
    4.9 MB) never enter memory (SA-0MSLVQMKF000ESPZ).

    Returns the parsed projection (typically an int or small dict).
    """
    full_cmd = list(cmd)
    if full_cmd and full_cmd[0] == "wl":
        full_cmd[1:1] = _resolve_worklog_flags(full_cmd, explicit_dir=worklog_dir)
    shell_cmd = " ".join(shlex.quote(part) for part in full_cmd)
    shell_cmd += f" | jq -c {shlex.quote(jq_expr)}"
    proc = runner(["bash", "-c", shell_cmd])
    if proc.returncode != 0:
        raise RuntimeError(
            f"wl command failed ({shell_cmd}): {_wl_error_detail(proc)}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from wl projection: {exc}") from exc


def _detect_project_root() -> Path:
    """Detect the project root directory.

    Tries ``git rev-parse --show-toplevel`` first. If that fails
    (not a git repo, git not available, etc.), falls back to
    ``Path.cwd()``.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(proc.stdout.strip())
    except (subprocess.CalledProcessError, OSError):
        return Path.cwd()


TARGET_PROJECT_ROOT: Path = _detect_project_root()
"""Project root targeted by the audit runner.

Defaults to the git root (or ``Path.cwd()`` as fallback) at import time.
This may differ from ``REPO_ROOT`` when the audit runner's framework
repository is not the working directory.
"""


SIBLING_SCAN_ROOT: Path = SHARED_SIBLING_SCAN_ROOT
"""Compatibility alias for the shared prefix-to-sibling scan root.

The scan itself is implemented in ``skill.shared.status_lifecycle``
(SA-0MSG57UNY009DE51); this alias preserves the historical name for
callers that referenced ``audit_runner.SIBLING_SCAN_ROOT``. Patch the
shared ``skill.shared.status_lifecycle.SIBLING_SCAN_ROOT`` constant in
tests — the resolution no longer reads this module attribute.

Sibling projects (whose ``.worklog/config.yaml`` carries a ``prefix:`` marker)
live alongside the framework repository that ships this runner, i.e. under
``REPO_ROOT.parent``. Basing the scan on ``REPO_ROOT`` — a cwd-independent
path derived from this module's own location — rather than the import-time
cwd-derived ``TARGET_PROJECT_ROOT.parent`` keeps worklog resolution correct
when the runner is launched from the skill install directory or any other
non-project cwd (SA-0MSG48MEI0083K82).
"""


# ---------------------------------------------------------------------------
# Launch-context guard (LP-0MSQ32HNR007AI6B)
# ---------------------------------------------------------------------------


class AuditScopeError(Exception):
    """Raised when an audit resolves a wrong project/file scope.

    Distinct from genuine audit failures: a scope error means the launch
    context (cwd-derived project root, or the Phase 2 FILE SCOPE manifest)
    does not own the audited work item — the run must abort loudly instead
    of producing a misleading all-unmet report (incident: an audit of
    LP-0MSORQVK50012Q4D launched from the SorraAgents cwd ran Phase 2
    against the audit skill's own tree and wasted ~124 min of model time).
    """


def _resolve_owning_project_root(issue_id: str,
                                 worklog_dir: str | None = None) -> Path | None:
    """Resolve the project root expected to own *issue_id*.

    Precedence mirrors the shared wl worklog resolution (explicit dir >
    prefix-to-sibling scan): an explicit ``--worklog-dir`` overrides auto
    resolution (its parent is the expected project root); otherwise the
    item's id prefix is matched against sibling projects' ``.worklog``
    configs. Returns ``None`` when ownership cannot be determined — callers
    fail open rather than block (unknown prefix / no sibling match).
    """
    if worklog_dir:
        return Path(worklog_dir).resolve().parent
    prefix = _extract_work_item_prefix_shared([issue_id])
    if not prefix:
        return None
    wl_dir = _find_worklog_dir_by_prefix(prefix)
    if wl_dir is None:
        return None
    return wl_dir.parent


def _same_git_repository(root_a: Path, root_b: Path) -> bool:
    """True when *root_a* and *root_b* are checkouts of the same git repo.

    Compares resolved ``git rev-parse --git-common-dir`` outputs so a
    worktree of the owning project (e.g. ``<owning>/.worklog/worktrees/``)
    counts as the owning project. Returns False on any git failure — callers
    fall back to direct path comparison.
    """
    try:
        def _common_dir(root: Path) -> str | None:
            proc = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
                capture_output=True, text=True, check=False, timeout=30,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return None
            return str((root / proc.stdout.strip()).resolve())
        a = _common_dir(root_a)
        b = _common_dir(root_b)
        return a is not None and a == b
    except (OSError, subprocess.SubprocessError):
        return False


def _verify_launch_context(issue_id: str,
                           worklog_dir: str | None = None) -> str | None:
    """Return a fatal error message when the launch context does not own *issue_id*.

    The launch context is the cwd-derived ``TARGET_PROJECT_ROOT`` — the
    repository that Phase 1/2 would target. The owning project is resolved
    from the worklog (explicit ``--worklog-dir`` parent, else
    prefix-to-sibling scan). A mismatch (e.g. an LP item audited from the
    SorraAgents checkout) means the run would scope its FILE SCOPE manifest
    to the wrong repository and emit misleading 'unmet' verdicts — fail
    fast with zero pi calls.

    Returns ``None`` when the context is correct (or ownership cannot be
    determined — fail open).
    """
    owning_root = _resolve_owning_project_root(issue_id, worklog_dir=worklog_dir)
    if owning_root is None:
        return None
    launch_root = TARGET_PROJECT_ROOT.resolve()
    if launch_root == owning_root.resolve():
        return None
    if _same_git_repository(launch_root, owning_root):
        # A worktree of the owning project is a legitimate same-repo launch.
        return None
    # Fallback when git is unavailable (e.g. a mocked subprocess in tests):
    # the implement-worktree convention places worktrees under
    # <owning>/.worklog/worktrees/ — same repository as the owning project.
    if (owning_root / ".worklog" / "worktrees") in launch_root.parents:
        return None
    return (
        f"Audit launch-context error: work item {issue_id} is owned by "
        f"project {owning_root} (resolved from the worklog prefix scan), "
        f"but this run was launched from project {launch_root}. The audit "
        f"would target the wrong repository and waste model time. Re-launch "
        f"from {owning_root} — the project that owns the item. Note: "
        f"--worklog-dir does not change the project scope; only the launch "
        f"directory does."
    )


def _project_top_levels(project_root: Path) -> list[str]:
    """Return the tracked top-level paths of *project_root* (git ls-files).

    Falls back to a directory listing when git is unavailable. Returns an
    empty list on any failure (callers fail open). Dot-entries are excluded
    (they are never manifest markers).
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "ls-files"],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if proc.returncode == 0 and proc.stdout:
            tops: set[str] = set()
            for ln in proc.stdout.splitlines():
                rel = ln.strip()
                if not rel:
                    continue
                top = rel.split("/", 1)[0] if "/" in rel else rel
                if not top.startswith("."):
                    tops.add(top)
            return sorted(tops)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return sorted(
            e.name for e in project_root.iterdir()
            if not e.name.startswith(".")
        )
    except OSError:
        return []


def _distinctive_project_top_levels(owning_root: Path) -> list[str]:
    """Return *owning_root*'s top-level entries not shared with the framework.

    The framework repo (``REPO_ROOT``) ships the audit skill; its top-level
    layout (skill/, tests/, docs/, scripts/, ...) can appear in a manifest
    built from the audit skill's own tree. Distinctive entries are markers
    that prove the manifest was built from the item's repository. An empty
    list means the owning project IS the framework repo (mono-repo) or has
    no verifiable marker — callers fail open.
    """
    owning_tops = set(_project_top_levels(owning_root))
    framework_tops = set(_project_top_levels(REPO_ROOT))
    return sorted(owning_tops - framework_tops)


def _validate_file_scope_manifest(file_scope: str,
                                  owning_root: Path | None) -> str | None:
    """Validate the FILE SCOPE manifest covers the work item's repository.

    Returns an error message when the manifest does NOT reference the owning
    project's files (e.g. it was built from the audit skill's own tree after
    a mis-scoped launch), or ``None`` when valid / unverifiable (fail-open).

    The check is inclusion-based (per the risk mitigation): at least one
    distinctive top-level entry of the item repo must appear in the manifest
    — the manifest need not equal the item repo, so mono-repo items whose
    files legitimately live in the framework tree stay valid.
    """
    if owning_root is None:
        return None
    distinctive = _distinctive_project_top_levels(owning_root)
    if not distinctive:
        return None  # nothing distinctive to verify against — fail open
    manifest_lower = file_scope.lower()
    if any(entry.lower() in manifest_lower for entry in distinctive):
        return None
    return (
        f"Audit scope error: the Phase 2 FILE SCOPE manifest does not contain "
        f"the work item repository files (owning project: {owning_root}). The "
        f"resolved scope is the audit skill's own tree or another repository; "
        f"re-launch the audit from {owning_root} and re-run."
    )


# ---------------------------------------------------------------------------
# Freshness gate
# ---------------------------------------------------------------------------


def _parse_iso_utc(value) -> datetime | None:
    """Parse an ISO-8601 timestamp into a tz-aware UTC ``datetime``.

    Normalizes the ``Z`` suffix for Python 3.10 compatibility, parses via
    ``datetime.fromisoformat``, and treats naive timestamps as UTC. Returns
    ``None`` when the value cannot be parsed (``ValueError``/``TypeError``
    swallowed). Shared by the freshness gates in ``_check_audit_freshness``
    and ``_get_child_audit_verdict`` (SA-0MSL1Z70C007B9VZ) — the single
    timestamp-parse implementation both callers use.
    """
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _audit_time_is_fresh(audit_time: datetime, update_time: datetime) -> bool:
    """Whether an audit is newer than the freshness threshold.

    True when ``audit_time > update_time + AUDIT_FRESHNESS_BUFFER_SECONDS``,
    i.e. the audit was persisted after the item's last update plus the
    freshness buffer. Shared by the item-level and child freshness gates
    (SA-0MSL1Z70C007B9VZ); the child gate additionally applies the
    persistence-write tolerance when this returns False.
    """
    threshold = update_time + timedelta(seconds=AUDIT_FRESHNESS_BUFFER_SECONDS)
    return audit_time > threshold


def _check_audit_freshness(runner: Runner, issue_id: str,
                           worklog_dir: str | None = None,
                           work_item: dict | None = None) -> str | None:
    """Check if there's a fresh audit for the work item.

    Two-stage freshness gate:

    1. **Content-based gate (primary, SA-0MSKB6US1009CNHT):** when the
       stored audit report carries a content fingerprint (git HEAD sha +
       work-item description hash + Key Files list captured at audit time),
       the audit is fresh iff the fingerprint is unchanged. This makes
       re-audits of unchanged items return the existing report in seconds
       instead of re-running the pipeline, even when ``updatedAt`` moved for
       non-content reasons (e.g. a comment was added). A change in ANY
       fingerprint component invalidates freshness and re-runs the pipeline.
    2. **Time gate (floor):** when the stored report carries no fingerprint
       (e.g. legacy audits persisted before the fingerprint feature), the
       existing 60s time gate is retained as the freshness floor: compare
       the audit's ``auditedAt`` against the work item's ``updatedAt`` plus
       ``AUDIT_FRESHNESS_BUFFER_SECONDS``.

    Returns the ``rawOutput`` of the existing audit if still fresh, ``None``
    otherwise (no prior audit, stale audit, or command failure).

    The gate gracefully falls through on any failure (no audit data, command
    error, parse error) so that the normal audit pipeline always runs when
    freshness cannot be determined.

    *work_item* may be passed in to avoid a redundant ``wl show`` when the
    caller already fetched the work item (SA-0MSL1Z7E9005TLBA): it is used
    for the fingerprint computation and the time-gate ``updatedAt``.
    When omitted, the work item is fetched via ``wl show``.
    """

    try:
        data = _run_wl(runner, ["wl", "audit-show", issue_id, "--json"],
                       worklog_dir=worklog_dir)
    except RuntimeError:
        return None  # No audit data or command failure

    if not isinstance(data, dict) or data.get("success") is False:
        return None

    audit = data.get("audit")
    if not audit:
        return None  # No prior audit

    audited_at = audit.get("auditedAt")
    raw_output = audit.get("rawOutput")
    if not audited_at or not raw_output:
        return None

    # ── Content-based freshness gate (SA-0MSKB6US1009CNHT) ─────────────
    # When the stored audit carries a content fingerprint, freshness is
    # decided by content match: re-auditing an item whose git HEAD sha,
    # description hash, and Key Files are unchanged returns the existing
    # report in seconds instead of re-running the pipeline.
    stored_fingerprint = _extract_content_fingerprint(raw_output)
    if stored_fingerprint is not None:
        current_fingerprint = _compute_content_fingerprint(
            runner, issue_id, worklog_dir=worklog_dir, work_item=work_item,
        )
        if current_fingerprint is None:
            # Fingerprint cannot be computed now (e.g. git unavailable) —
            # fail open and let the pipeline re-run.
            return None
        if current_fingerprint == stored_fingerprint:
            return raw_output
        # Content changed → stale → re-run the pipeline.
        return None

    # ── Time gate (floor): legacy audits without a fingerprint ──────────
    # Get the work item's updatedAt (reuse an already-fetched work_item
    # when the caller has it — SA-0MSL1Z7E9005TLBA).
    if work_item is None:
        try:
            wi_data = _run_wl(runner, ["wl", "show", issue_id, "--json"],
                              worklog_dir=worklog_dir)
        except RuntimeError:
            return None

        work_item = wi_data.get("workItem", {}) if isinstance(wi_data, dict) else {}
    updated_at = work_item.get("updatedAt")
    if not updated_at:
        return None

    # Compare ISO-8601 timestamps
    audit_time = _parse_iso_utc(audited_at)
    update_time = _parse_iso_utc(updated_at)
    if audit_time is None or update_time is None:
        return None  # Unparseable timestamps → not fresh (fail open)

    if _audit_time_is_fresh(audit_time, update_time):
        return raw_output

    return None


def _extract_content_fingerprint(report_text: str) -> str | None:
    """Extract the content fingerprint from a persisted audit report.

    The fingerprint is stored as a metadata line in the report:
    ``Audit content fingerprint: <sha256-hex>``. Returns the fingerprint
    value, or ``None`` when the report carries no fingerprint (e.g. legacy
    audits persisted before SA-0MSKB6US1009CNHT, or manual reports).
    """
    if not report_text:
        return None
    for line in report_text.splitlines():
        if line.startswith(AUDIT_CONTENT_FINGERPRINT_PREFIX):
            value = line[len(AUDIT_CONTENT_FINGERPRINT_PREFIX):].strip()
            return value or None
    return None


def _compute_content_fingerprint(runner: Runner, issue_id: str,
                                 worklog_dir: str | None = None,
                                 work_item: dict | None = None) -> str | None:
    """Compute the content fingerprint for a work item at the current state.

    The fingerprint combines four components so that a change in ANY of them
    invalidates freshness (SA-0MSKB6US1009CNHT, SA-0MSL1YXG7004F2BZ):

    1. **git HEAD sha** — the repository state being audited.
    2. **work-item description hash** — the audited acceptance criteria text.
    3. **Key Files list** — the file-scope manifest of the audited item.
    4. **working-tree state** — a hash of ``git status --porcelain`` +
       ``git diff --name-only HEAD`` output, so uncommitted/untracked
       changes between audits invalidate freshness (the audit reads the
       working tree, not just HEAD). Degrades to an empty marker when git
       is unavailable so fail-open callers keep working.

    *work_item* may be passed in to avoid a redundant ``wl show`` call when
    the caller already fetched the work item (e.g. ``cmd_issue``). When
    omitted, the work item is fetched via ``wl show``.

    Returns a sha256 hex digest of the canonical JSON payload, or ``None``
    when the fingerprint cannot be determined (git unavailable, wl show
    failure, or missing data) so callers fail open and re-run the pipeline.
    """
    head_sha = _resolve_audited_head(runner)
    if head_sha is None:
        return None
    if work_item is None:
        try:
            data = _run_wl(runner, ["wl", "show", issue_id, "--json"],
                           worklog_dir=worklog_dir)
        except RuntimeError:
            return None
        work_item = data.get("workItem", {}) if isinstance(data, dict) else {}
    description = work_item.get("description", "") or ""
    key_files = _extract_key_files(description)
    payload = json.dumps({
        "head_sha": head_sha,
        "description_hash": hashlib.sha256(description.encode("utf-8")).hexdigest(),
        "key_files": key_files,
        "working_tree_hash": _resolve_working_tree_hash(runner),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_working_tree_hash(runner: Runner) -> str:
    """Hash the working-tree state for the fingerprint (SA-0MSL1YXG7004F2BZ).

    Combines ``git status --porcelain`` (untracked + unstaged changes) with
    ``git diff --name-only HEAD`` (staged changes) into a deterministic
    sorted payload, hashed with sha256. Returns a fixed empty-string marker
    when git is unavailable so the fingerprint still works fail-open.
    """
    lines: list[str] = []
    for cmd in (["git", "status", "--porcelain"], ["git", "diff", "--name-only", "HEAD"]):
        try:
            proc = runner(cmd)
        except Exception:  # noqa: S112, BLE001 -- git is best-effort; swallow to stay fail-open
            continue
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                line = line.strip()
                if line:
                    lines.append(line)
    if not lines:
        return ""
    payload = "\n".join(sorted(set(lines)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Config loading (following Ralph's _load_config / _deep_merge pattern)
# ---------------------------------------------------------------------------


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep-merge override into base, returning a new dict."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_asset_config() -> dict:
    """Load the shipped default config from skill/ralph/assets/.ralph.json."""
    try:
        with open(ASSET_CONFIG_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _load_config() -> dict:
    """Load config merging asset defaults with CWD config file.

    Asset defaults from skill/ralph/assets/.ralph.json are the base.
    A .ralph.json or ralph.config.json in the current working directory
    overrides those values. CLI flags take highest precedence downstream.
    """
    config = _load_asset_config()

    for path in RALPH_CONFIG_FILES:
        if not path.exists():
            continue
        if path.suffix == ".json":
            try:
                with open(path) as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    config = _deep_merge(config, data)
            except (json.JSONDecodeError, OSError):
                pass

    return config


def _default_parent_timeout(n_children: int) -> int:
    """Compute the default cumulative elapsed-time guard (seconds).

    Scales with the number of active children so a multi-child parent gets a
    realistic default budget: ``PARENT_TIMEOUT_DEFAULT + n_children x
    PARENT_TIMEOUT_PER_CHILD``. A single-child parent keeps a ~710s budget and
    a 10-child parent gets ~6,110s instead of the old fixed 110s that skipped
    every child after the parent Phase 1 call. Explicit ``--parent-timeout``
    / ``AUDIT_PARENT_TIMEOUT`` overrides replace this computed value entirely.
    """
    return PARENT_TIMEOUT_DEFAULT + n_children * PARENT_TIMEOUT_PER_CHILD


def _resolve_max_child_audits(cli_value: int | None = None) -> int:
    """Resolve the per-run cap on auto-triggered recursive child audits.

    Precedence:
      1. ``--max-child-audits`` CLI flag (explicit override)
      2. ``AUDIT_MAX_CHILD_AUDITS`` environment variable (positive integer)
      3. ``_DEFAULT_MAX_CHILD_AUDITS``

    An invalid env value (non-positive integer) is ignored with a warning so
    a misconfigured environment cannot break the audit run.
    """
    if cli_value is not None:
        if cli_value < 1:
            print(
                f"Warning: invalid --max-child-audits value {cli_value}; "
                f"using the default cap ({_DEFAULT_MAX_CHILD_AUDITS})",
                file=sys.stderr,
            )
            return _DEFAULT_MAX_CHILD_AUDITS
        return cli_value
    env_value = os.environ.get(AUDIT_MAX_CHILD_AUDITS_ENV)
    if env_value:
        try:
            parsed = int(env_value)
            if parsed < 1:
                raise ValueError
            return parsed
        except ValueError:
            print(
                f"Warning: invalid {AUDIT_MAX_CHILD_AUDITS_ENV} value "
                f"{env_value!r}; using the default cap "
                f"({_DEFAULT_MAX_CHILD_AUDITS})",
                file=sys.stderr,
            )
    return _DEFAULT_MAX_CHILD_AUDITS


def _resolve_max_citations_per_ac(cli_value: int | None = None) -> int:
    """Resolve the max file:line citations per AC for Phase 2 deep prompts.

    Precedence:
      1. ``--max-citations-per-ac`` CLI flag (explicit override)
      2. ``audit.max_citations_per_ac`` key in the CWD ``.ralph.json`` /
         ``ralph.config.json`` (dotted form first, then the nested
         ``audit: {max_citations_per_ac: N}`` form)
      3. ``_DEFAULT_MAX_CITATIONS_PER_AC`` (5)

    An invalid value (non-positive integer) fails closed to the default with
    a warning so a misconfigured config cannot break the audit run (mirrors
    ``_resolve_max_child_audits``; LP-0MSQ32WM5000NCB7 AC1/AC6).
    """
    if cli_value is not None:
        if cli_value < 1:
            print(
                f"Warning: invalid --max-citations-per-ac value {cli_value}; "
                f"using the default cap ({_DEFAULT_MAX_CITATIONS_PER_AC})",
                file=sys.stderr,
            )
            return _DEFAULT_MAX_CITATIONS_PER_AC
        return cli_value
    config = _load_config()
    config_value = config.get("audit.max_citations_per_ac")
    if config_value is None:
        audit_section = config.get("audit")
        if isinstance(audit_section, dict):
            config_value = audit_section.get("max_citations_per_ac")
    if config_value is not None:
        try:
            parsed = int(config_value)
            if parsed < 1:
                raise ValueError
            return parsed
        except (ValueError, TypeError):
            print(
                f"Warning: invalid audit.max_citations_per_ac value "
                f"{config_value!r}; using the default cap "
                f"({_DEFAULT_MAX_CITATIONS_PER_AC})",
                file=sys.stderr,
            )
    return _DEFAULT_MAX_CITATIONS_PER_AC


def _max_citations_prompt_snippet(max_citations: int) -> str:
    """Build the Phase-2 evidence citation-cap instruction.

    Injected into all three deep-analysis prompts (parent ``phase2_deep``,
    child ``phase2_child``, batch ``phase2_batch``) so evidence-JSON
    generation is bounded while every AC keeps at least one specific
    file:line reference (LP-0MSQ32WM5000NCB7 AC1/AC4). Prompt-level only:
    never mutates parsed evidence or verdicts; the canonical report format
    is untouched.
    """
    return (
        f"EVIDENCE SCOPE — For each criterion, cite AT MOST "
        f"{max_citations} specific file:line references as evidence "
        f"(minimum 1). Prefer fewer, stronger references over exhaustive "
        f"lists; this bound keeps deep analysis fast.\n"
    )


def _resolve_parent_timeout(cli_value: int | None) -> int | None:
    """Resolve an explicit cumulative elapsed-time guard override (seconds).

    Precedence:
      1. ``--parent-timeout`` CLI flag (explicit override)
      2. ``AUDIT_PARENT_TIMEOUT`` environment variable (seconds)
      3. ``None`` — no override: ``cmd_issue`` computes a default guard that
         scales with the number of active children
         (``_default_parent_timeout(N)``).

    An invalid ``AUDIT_PARENT_TIMEOUT`` value (non-integer) is ignored with a
    warning so a misconfigured environment cannot break the audit run.
    """
    if cli_value is not None:
        return int(cli_value)
    env_value = os.environ.get(AUDIT_PARENT_TIMEOUT_ENV)
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            print(
                f"Warning: invalid {AUDIT_PARENT_TIMEOUT_ENV} value {env_value!r}; "
                "using the scaled default guard "
                f"({PARENT_TIMEOUT_DEFAULT}s + N x {PARENT_TIMEOUT_PER_CHILD}s)",
                file=sys.stderr,
            )
    return None


def _resolve_effective_timeout(cli_timeout: int | None) -> int | None:
    """Resolve the effective per-call Pi timeout in seconds.

    Precedence:
      1. ``--timeout`` CLI flag (explicit override)
      2. ``AUDIT_PI_TIMEOUT`` environment variable (seconds)
      3. ``None`` — falls back to ``CALL_PI_TIMEOUT`` inside ``_call_pi``

    An invalid ``AUDIT_PI_TIMEOUT`` value (non-integer) is ignored with a
    warning so a misconfigured environment cannot break the audit run.
    """
    if cli_timeout is not None:
        return cli_timeout
    env_value = os.environ.get(AUDIT_PI_TIMEOUT_ENV)
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            print(
                f"Warning: invalid {AUDIT_PI_TIMEOUT_ENV} value {env_value!r}; "
                "using default timeout",
                file=sys.stderr,
            )
    return None


def _resolve_child_screen_timeout(cli_timeout: int | None) -> int | None:
    """Resolve the per-call budget for child Phase-1 AC-review screens.

    Precedence:
      1. ``--child-screen-timeout`` CLI flag (explicit override)
      2. ``AUDIT_CHILD_SCREEN_TIMEOUT`` environment variable (seconds)
      3. ``None`` — falls back to ``_CHILD_SCREEN_TIMEOUT_DEFAULT`` (600 s)
         inside ``_call_pi`` for child screens

    An invalid value (non-integer) is ignored with a warning so a
    misconfigured environment cannot break the audit run.
    """
    if cli_timeout is not None:
        return cli_timeout
    env_value = os.environ.get(AUDIT_CHILD_SCREEN_TIMEOUT_ENV)
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            print(
                f"Warning: invalid {AUDIT_CHILD_SCREEN_TIMEOUT_ENV} value {env_value!r}; "
                "using default child-screen timeout",
                file=sys.stderr,
            )
    return None


def _resolve_stall_timeout() -> int:
    """Resolve the in-process stall-abort threshold in seconds.

    Precedence:
      1. ``AUDIT_STALL_TIMEOUT`` environment variable (seconds)
      2. ``_STALL_TIMEOUT_DEFAULT`` (600)

    An invalid value (non-integer) is ignored with a warning so a
    misconfigured environment cannot break the audit run.
    """
    env_value = os.environ.get(AUDIT_STALL_TIMEOUT_ENV)
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            print(
                f"Warning: invalid {AUDIT_STALL_TIMEOUT_ENV} value {env_value!r}; "
                "using default stall timeout",
                file=sys.stderr,
            )
    return _STALL_TIMEOUT_DEFAULT


def _green_run_prompt_block(sha: str) -> str:
    """Build the GREEN-RUN attestation block injected into audit prompts.

    The block tells the model that the operator attests the full project test
    suite passed at *sha* (the audited HEAD), so execution-dependent criteria
    (e.g. 'full test suite passes') MAY be marked met based on that
    attestation — while the read-only mandate otherwise remains in force and
    the suite must NOT be executed. Returns a string ending in a blank line
    so callers can splice it between existing prompt sections.
    """
    return (
        "GREEN-RUN ATTESTATION — The operator attests the full project test "
        f"suite passed at commit {sha} (== current HEAD). "
        "Execution-dependent criteria (e.g. 'full test suite passes') MAY be "
        "marked met based on this attestation. Do NOT execute the test suite "
        "or any other state-modifying command — the read-only mandate "
        "otherwise remains in force.\n\n"
    )


def _resolve_green_run_value(cli_value: str | None) -> str | None:
    """Resolve the raw green-run value (exact sha or ``HEAD`` alias).

    Precedence:
      1. ``--green-run`` CLI flag (explicit override)
      2. ``AUDIT_GREEN_RUN`` environment variable
      3. ``None`` — no attestation

    Empty/whitespace-only values are treated as unset.
    """
    if cli_value is not None:
        return cli_value.strip() or None
    env_value = os.environ.get(AUDIT_GREEN_RUN_ENV, "")
    return env_value.strip() or None


def _resolve_audited_head(runner: Runner) -> str | None:
    """Resolve the audited HEAD commit sha via ``git rev-parse HEAD``.

    Returns the full sha, or ``None`` when git is unavailable / the runner
    fails (graceful fallback — a missing HEAD simply means no attestation is
    accepted; execution-dependent ACs stay partial).
    """
    try:
        proc = runner(["git", "rev-parse", "HEAD"])
    except Exception:  # noqa: BLE001 -- git is best-effort for the attestation
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def _resolve_green_run_attestation(
    cli_value: str | None, runner: Runner,
) -> tuple[str | None, str | None]:
    """Resolve and validate the operator-attested green test run.

    Returns ``(prompt_block, attested_sha)``. Both are ``None`` when there is
    no attestation (no flag/env) or when the attestation cannot be validated:
    a mismatched sha, an unresolvable ``HEAD`` alias, or git being unavailable
    all print a clear error to stderr and yield NO attestation — so
    execution-dependent ACs stay partial (never silently accepted).
    """
    value = _resolve_green_run_value(cli_value)
    if value is None:
        return None, None

    head_sha = _resolve_audited_head(runner)
    if head_sha is None:
        print(
            f"Error: --green-run/AUDIT_GREEN_RUN value {value!r} cannot be "
            "verified: the audited HEAD could not be resolved (git "
            "unavailable or not a git repository). No attestation accepted — "
            "execution-dependent acceptance criteria stay partial.",
            file=sys.stderr,
        )
        return None, None

    if value == "HEAD" or value == head_sha:
        return _green_run_prompt_block(head_sha), head_sha

    print(
        f"Error: --green-run/AUDIT_GREEN_RUN value {value!r} does not match "
        f"the audited HEAD {head_sha!r}. Attestation rejected — "
        "execution-dependent acceptance criteria stay partial. Re-run with "
        "the exact HEAD sha or the alias 'HEAD'.",
        file=sys.stderr,
    )
    return None, None


def _auto_green_run_prompt_block(sha: str) -> str:
    """Build the AUTO-VERIFIED full-suite block injected into audit prompts.

    The block tells the model that a full test-suite run at *sha* (the audited
    HEAD) was verified green from the per-repo test cache via a READ-ONLY
    query (``query_cached`` never executes anything), so execution-dependent
    criteria (e.g. 'full test suite passes') MAY be marked met based on that
    verified cached result — while the read-only mandate otherwise remains in
    force and the suite must NOT be executed. Returns a string ending in a
    blank line so callers can splice it between existing prompt sections.
    """
    return (
        f"{AUTO_GREEN_RUN_BLOCK_HEADER} — A cached full test-suite run at "
        f"commit {sha} (== current HEAD) was verified green from the per-repo "
        "test cache (read-only query; the audit never executes the suite). "
        "Execution-dependent criteria (e.g. 'full test suite passes') MAY be "
        "marked met based on this verified cached result. Do NOT execute the "
        "test suite or any other state-modifying command — the read-only "
        "mandate otherwise remains in force.\n\n"
    )


# ---------------------------------------------------------------------------
# Full-suite cache classification (shared by the automatic verification path
# and the pre-flight gate, SA-0MSQ72BVV0011SRU)
# ---------------------------------------------------------------------------

# Classification statuses returned by _classify_full_suite_cache.
_FULL_SUITE_CACHE_GREEN = "green"
_FULL_SUITE_CACHE_MISS = "miss"
_FULL_SUITE_CACHE_RED = "red"
_FULL_SUITE_CACHE_ERROR = "error"

# pytest config markers, mirroring implement.py's _has_pytest_markers so the
# audit and the implement/test skills agree on whether a repo has a pytest
# suite (SA-0MSQ72BVV0011SRU AC3).
_PYTEST_CONFIG_MARKERS = (
    ("pyproject.toml", "[tool.pytest.ini_options]"),
    ("setup.cfg", "[tool:pytest]"),
    ("tox.ini", "[pytest]"),
)


def _repo_has_pytest_suite(project_root: Path) -> bool:
    """Whether *project_root* declares a runnable pytest suite.

    True when the repo carries a pytest config marker (pytest.ini, or the
    pytest section of pyproject.toml / setup.cfg / tox.ini) or pytest-style
    test files (``tests/``/``test/`` dirs containing ``*.py``, or root-level
    ``test_*.py`` / ``*_test.py``). Importability is deliberately NOT probed:
    the audit process runs from the framework interpreter which always
    provides pytest, so the presence of markers or test files is the
    discriminator that decides whether the pytest suite command applies to
    this repo.

    Mirrors implement.py's pytest-suite detection so a repo the implement
    skill treats as having no pytest suite is never falsely gated on a
    phantom pytest cache miss (AC3).
    """
    if (project_root / "pytest.ini").is_file():
        return True
    for name, marker in _PYTEST_CONFIG_MARKERS:
        path = project_root / name
        if not path.is_file():
            continue
        try:
            if marker in path.read_text(encoding="utf-8"):
                return True
        except OSError:
            continue
    for dirname in ("tests", "test"):
        test_dir = project_root / dirname
        if test_dir.is_dir() and any(test_dir.rglob("*.py")):
            return True
    return bool(
        any(project_root.glob("test_*.py")) or any(project_root.glob("*_test.py"))
    )


def _effective_suite_commands(project_root: Path) -> list[str]:
    """The full-suite command set that actually applies to *project_root*.

    ``full_suite_commands`` is already node-dir-aware: node suite commands
    are emitted only for ``tests/node``, ``tests/cli`` and ``tests/unit``
    dirs that exist in the repo (SA-0MSJELL44009XYIL). The pytest command is
    additionally dropped when the repo does not declare a pytest suite
    (SA-0MSQ72BVV0011SRU AC3) — a phantom pytest command would otherwise
    produce a permanent cache miss that falsely blocks audits of no-pytest
    repos (e.g. a docs-only repo) even after the real suites ran green.
    """
    commands = full_suite_commands(project_root)
    if _repo_has_pytest_suite(project_root):
        return commands
    pytest_cmd = pytest_command()
    return [c for c in commands if c != pytest_cmd]


def _classify_full_suite_cache(
    runner: Runner,
    cwd: str | Path | None = None,
    commands: list[str] | None = None,
) -> tuple[str, str | None, list[str]]:
    """Classify the read-only full-suite cache state at *cwd*.

    Queries the per-repo test cache (``query_cached``) for each command in
    the canonical full-suite set at *cwd* (default ``TARGET_PROJECT_ROOT``)
    — never executing anything, so the audit's read-only mandate is preserved
    unconditionally (SA-0MSIU5HFI0024D7W). *commands* overrides the set
    (the pre-flight gate passes ``_effective_suite_commands``); the default
    is ``full_suite_commands`` — already node-dir-aware
    (SA-0MSJELL44009XYIL), so a repo without tests/node is never asked
    about a phantom tests/node command.

    Returns ``(status, head_sha, problems)``:

    - ``"green"`` — EVERY command has a cached entry at the audited git
      state within the cache TTL AND every entry's exit code is 0
      (``head_sha`` set, ``problems`` empty).
    - ``"miss"``  — at least one command has NO cached entry at HEAD
      (``head_sha`` set; ``problems`` name the missing commands). This is
      the state the pre-flight gate blocks on.
    - ``"red"``   — every command is cached but at least one entry exited
      non-zero (``head_sha`` set; ``problems`` name the failing commands).
      Keeps the historical partial + diagnostic behavior — NOT a gate state.
    - ``"error"`` — HEAD could not be resolved (``head_sha`` None). A cache
      query exception propagates to the caller, which decides fail-closed
      (``_resolve_auto_green_run``) vs fail-open (the pre-flight gate).
    """
    head_sha = _resolve_audited_head(runner)
    if head_sha is None:
        return _FULL_SUITE_CACHE_ERROR, None, []

    project_root = Path(cwd or TARGET_PROJECT_ROOT).resolve()
    if commands is None:
        commands = full_suite_commands(project_root)
    problems: list[str] = []
    for command in commands:
        entry = query_cached(
            command, cwd=str(project_root), ttl=DEFAULT_TTL_SECONDS,
        )
        if entry is None:
            problems.append(
                f"no cached full-suite run for '{command}' at HEAD {head_sha}"
            )
        elif int(entry.get("exit_code", -1)) != 0:
            problems.append(
                f"cached full-suite run for '{command}' exited non-zero "
                f"({int(entry.get('exit_code'))})"
            )
    if not problems:
        return _FULL_SUITE_CACHE_GREEN, head_sha, []
    if any(p.startswith("no cached full-suite run") for p in problems):
        return _FULL_SUITE_CACHE_MISS, head_sha, problems
    return _FULL_SUITE_CACHE_RED, head_sha, problems


def _resolve_auto_green_run(
    runner: Runner,
    cwd: str | Path | None = None,
) -> tuple[str | None, str | None]:
    """Resolve an automatically-verified green full-suite run, read-only.

    Delegates the cache query to :func:`_classify_full_suite_cache` (the
    single source of truth shared with the pre-flight gate) and maps the
    classification onto the historical contract:

    Returns ``(prompt_block, head_sha)`` when the cache is GREEN (every
    suite command has a cached entry at the audited git state within the
    cache TTL AND every entry's exit code is 0). Otherwise prints a clear
    diagnostic to stderr — distinguishing a cache miss ('run /skill:test
    once to populate the cache') from a non-zero cached run ('suite is red;
    fix or attest with --green-run HEAD') — and returns ``(None, None)``,
    fail-closed: no evidence, the audit completes normally, and
    execution-dependent ACs stay partial.
    """
    try:
        status, head_sha, problems = _classify_full_suite_cache(runner, cwd=cwd)
        if status == _FULL_SUITE_CACHE_GREEN and head_sha is not None:
            return _auto_green_run_prompt_block(head_sha), head_sha
        if head_sha is None:
            return None, None
        commands = full_suite_commands(Path(cwd or TARGET_PROJECT_ROOT).resolve())
        print(
            "Automatic full-suite verification unavailable: "
            f"{len(problems)} of {len(commands)} suite command(s) not "
            f"verifiably green at HEAD {head_sha}. " + "; ".join(problems)
            + ". Run the full suite once at this commit (/skill:test or "
            "run_tests.py --force) to populate the test cache, then re-audit "
            "— or attest manually with --green-run HEAD. Execution-dependent "
            "criteria stay partial.",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001 -- fail-closed: never crash the audit
        print(
            f"Automatic full-suite verification unavailable: {exc}. "
            "Execution-dependent criteria stay partial.",
            file=sys.stderr,
        )
    return None, None


def _preflight_cache_gate(
    runner: Runner,
    cwd: str | Path | None = None,
) -> str | None:
    """Return an actionable blocking message when the full-suite cache is MISSING at HEAD.

    The pre-flight gate (SA-0MSQ72BVV0011SRU): turns the cache-miss
    *diagnostic* into an *early exit* so the audit can never ship a report
    with degraded 'met' verdicts on execution-dependent ACs when no
    full-suite evidence exists and the caller did not explicitly opt out
    (``--run-tests`` executes the suite; ``--green-run`` attests it).

    Fires ONLY on a cache **miss** — at least one command in the repo's
    effective suite set has no cached run at HEAD. It is repo-aware: the
    pytest command is not required for repos without a pytest suite
    (``_effective_suite_commands``, AC3), so a docs-only or vitest-only repo
    is never falsely blocked. A **red** cached run keeps the historical
    partial + diagnostic behavior (evidence exists that the suite ran; it
    just failed), a **green** cache proceeds, and cache errors fail open
    (an infra hiccup must not block an audit that today proceeds partial).

    Returns the message for the caller to print (and exit non-zero with),
    or None to proceed.
    """
    project_root = Path(cwd or TARGET_PROJECT_ROOT).resolve()
    try:
        status, head_sha, problems = _classify_full_suite_cache(
            runner, cwd=str(project_root),
            commands=_effective_suite_commands(project_root),
        )
    except Exception:  # noqa: BLE001 -- fail-open: infra hiccups never block
        return None
    if status != _FULL_SUITE_CACHE_MISS:
        return None
    missing = [p for p in problems if p.startswith("no cached full-suite run")]
    detail = "; ".join(missing) if missing else "; ".join(problems)
    return (
        "Audit blocked: no green full-suite run is cached at HEAD "
        f"{head_sha or 'unknown'} (full-suite cache miss: {detail}). The "
        "audit cannot verify execution-dependent acceptance criteria "
        "without evidence, so it will NOT produce a report or persist an "
        "audit — degraded 'met' verdicts are not allowed. Run the full "
        "suite once at this commit (/skill:test or run_tests.py --force) to "
        "populate the test cache, then re-audit — or explicitly opt out "
        "with --run-tests (executes the suite and populates the cache) or "
        "--green-run HEAD (operator attestation). The pre-audit work-item "
        "status/stage were left untouched."
    )


def _test_skill_run_prompt_block(sha: str) -> str:
    """Build the TEST-SKILL RUN block injected into audit prompts.

    The block tells the model that the full project test suite was EXECUTED
    via the test skill (``run_tests.py``) at *sha* (== the audited HEAD) and
    passed (``--run-tests`` path, SA-0MSJELSWS002UF60), so execution-
    dependent criteria (e.g. 'full test suite passes') MAY be marked met
    based on that executed green run — while the read-only mandate otherwise
    remains in force and the suite must NOT be executed again. Returns a
    string ending in a blank line so callers can splice it between existing
    prompt sections.
    """
    return (
        f"{TEST_SKILL_RUN_BLOCK_HEADER} — The full project test suite was "
        f"executed via the test skill (/skill:test / run_tests.py) at commit "
        f"{sha} (== current HEAD) and passed (--run-tests). "
        "Execution-dependent criteria (e.g. 'full test suite passes') MAY be "
        "marked met based on this executed green run. Do NOT execute the test "
        "suite again or any other state-modifying command — the read-only "
        "mandate otherwise remains in force.\n\n"
    )


def _run_tests_via_test_skill(
    cwd: str | Path,
    timeout: int = AUDIT_TEST_SKILL_RUN_TIMEOUT,
    parent_work_item_id: str | None = None,
    head_sha: str | None = None,
) -> dict:
    """Execute the full project test suite via the test skill's runner.

    The ``--run-tests`` path (SA-0MSJELSWS002UF60): an explicit,
    operator-authorized deviation from the audit's read-only mandate. The
    runner delegates to the test skill's machinery (``run_tests.py``) —
    executing each command in ``full_suite_commands()`` at *cwd* in quiet
    mode through the per-repo test cache with ``force=True`` (execute fresh,
    refresh the stored entries so subsequent audits auto-verify).

    Failures are triaged per the test skill, never silently ignored: each
    structured failure record is passed to the triage helper
    (``check_or_create``), which links/creates a critical ``test-failure``
    work item for the failing test (as a child of *parent_work_item_id* when
    provided). A triage helper failure is recorded but never crashes the run.

    Returns a dict:
      success   — True iff every suite command was executed and exited 0
                  with no parsed failures
      results   — per-command execution results (``run_cached`` dicts)
      failures  — structured failure records (test_name, stdout_excerpt,
                  stack_trace, suite_command)
      triaged   — triage helper results per failure
      notice    — error string when the suite could not be executed at all
    """
    project_root = Path(cwd or TARGET_PROJECT_ROOT).resolve()
    print(
        f"Invoking test skill (run_tests.py) — --run-tests enabled: executing "
        f"the full project test suite at {project_root} in quiet mode "
        f"(cache refresh, per-command timeout {timeout}s)...",
        file=sys.stderr,
    )
    results: list[dict] = []
    failures: list[dict] = []
    triaged: list[dict] = []
    notice = ""
    for command in full_suite_commands(project_root):
        try:
            run = run_cached(
                command,
                cwd=str(project_root),
                force=True,  # execute fresh; refresh the cache entry
                ttl=DEFAULT_TTL_SECONDS,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            notice = f"command not found: {exc.filename}"
            break
        except subprocess.TimeoutExpired:
            notice = f"suite command timed out after {timeout}s: {command}"
            break
        except Exception as exc:  # noqa: BLE001 -- fail-closed: never crash the audit
            notice = f"suite execution error: {exc}"
            break
        results.append(run)
        output = f"{run.get('stdout', '')}\n{run.get('stderr', '')}"
        if "pytest" in command:
            cmd_failures = parse_pytest_failures(output)
        else:
            cmd_failures = parse_node_failures(output)
        for failure in cmd_failures:
            failure["suite_command"] = command
        failures.extend(cmd_failures)
        if int(run.get("exit_code", -1)) != 0 and not cmd_failures:
            # Non-zero exit with no parseable failure records (e.g. the suite
            # crashed before any test ran): record an entry-level failure so
            # the run is genuinely red, never silently green.
            failures.append({
                "test_name": f"<suite exited {run.get('exit_code')}>: {command}",
                "stdout_excerpt": output[:1000],
                "stack_trace": output[:1000],
                "suite_command": command,
            })

    success = bool(results) and not notice and not failures

    # Triage failures per the test skill (AC4) — never silently ignored.
    if failures:
        try:
            from skill.triage.scripts.check_or_create import check_or_create
        except ImportError:
            check_or_create = None
        for failure in failures:
            if check_or_create is None:
                triaged.append({
                    "test_name": failure.get("test_name", ""),
                    "error": "triage helper unavailable",
                })
                continue
            try:
                triaged.append(check_or_create({
                    "test_name": failure.get("test_name", ""),
                    "stdout_excerpt": failure.get("stdout_excerpt", ""),
                    "stack_trace": failure.get("stack_trace", ""),
                    "repo_path": str(project_root),
                    "parent_work_item_id": parent_work_item_id,
                    "commit_hash": head_sha,
                }))
            except Exception as exc:  # noqa: BLE001 -- triage must never crash the audit
                triaged.append({
                    "test_name": failure.get("test_name", ""),
                    "error": str(exc),
                })

    print(
        f"Test skill run completed: success={success} commands={len(results)} "
        f"failures={len(failures)} triaged={len(triaged)} "
        f"notice={notice or 'none'}",
        file=sys.stderr,
    )
    return {
        "success": success,
        "results": results,
        "failures": failures,
        "triaged": triaged,
        "notice": notice,
    }


def _audit_semaphore_max_workers(cli_value: int | None = None) -> int:
    """Resolve the audit concurrency ceiling.

    Precedence:
      1. ``--max-concurrency`` CLI flag (explicit override)
      2. ``AUDIT_MAX_CONCURRENCY`` environment variable
      3. ``DEFAULT_MAX_WORKERS`` (5)

    An invalid (non-integer) env value is ignored with a warning so a
    misconfigured environment cannot break the audit run.
    """
    if cli_value is not None:
        return max(1, int(cli_value))
    env_value = os.environ.get(ENV_MAX_WORKERS)
    if env_value:
        try:
            return max(1, int(env_value))
        except ValueError:
            print(
                f"Warning: invalid {ENV_MAX_WORKERS} value {env_value!r}; "
                "using default ceiling",
                file=sys.stderr,
            )
    return DEFAULT_MAX_WORKERS


def _audit_lock_timeout() -> float:
    """Resolve the wait for a free audit concurrency slot.

    Precedence: ``AUDIT_LOCK_TIMEOUT`` env var > fail-fast default (0s).
    Default 0s means a saturated ceiling fails immediately (see
    ``AUDIT_LOCK_TIMEOUT_DEFAULT``). An invalid value is ignored with
    a warning.
    """
    env_value = os.environ.get(AUDIT_LOCK_TIMEOUT_ENV)
    if env_value:
        try:
            return float(env_value)
        except ValueError:
            print(
                f"Warning: invalid {AUDIT_LOCK_TIMEOUT_ENV} value {env_value!r}; "
                "using default lock timeout",
                file=sys.stderr,
            )
    return AUDIT_LOCK_TIMEOUT_DEFAULT


def _acquire_audit_slot(max_concurrency: int | None = None) -> Semaphore:
    """Acquire one audit concurrency slot (shared across processes).

    Bounds concurrent pi/audit subprocesses host-wide via the shared
    flock-based semaphore (skill/shared/process_semaphore.py). Returns a
    held :class:`Semaphore` whose :meth:`release` must be called (or use
    it as a context manager) to free the slot.

    Raises:
        TimeoutError: When the ceiling stays saturated past the bounded
            wait (``AUDIT_LOCK_TIMEOUT``; default 0s = fail fast).
    """
    sem = Semaphore(
        AUDIT_SEMAPHORE_NAME,
        max_workers=_audit_semaphore_max_workers(max_concurrency),
        timeout=_audit_lock_timeout(),
    )
    sem.acquire()
    return sem


def _resolve_parallelism() -> int:
    """Resolve the bounded concurrency cap for child deep analysis.

    Precedence:
      1. ``AUDIT_PARALLELISM`` environment variable (integer >= 1)
      2. ``AUDIT_PHASE2_PARALLELISM`` (legacy fallback — integer >= 1)
      3. ``_PARALLELISM_DEFAULT`` (2)

    Values below 1 are clamped to 1. An invalid (non-integer) value is
    ignored with a warning so a misconfigured environment cannot break the
    audit run.
    """
    env_value = os.environ.get(AUDIT_PARALLELISM_ENV)
    if not env_value:
        # Legacy fallback: honor the old name when the new one is unset.
        env_value = os.environ.get(AUDIT_PHASE2_PARALLELISM_ENV_LEGACY)
    if env_value:
        try:
            parsed = int(env_value)
            return max(1, parsed)
        except ValueError:
            print(
                f"Warning: invalid {AUDIT_PARALLELISM_ENV} value {env_value!r}; "
                "using default parallelism",
                file=sys.stderr,
            )
    return _PARALLELISM_DEFAULT


def _query_slot_status(url: str | None = None,
                       timeout: float = AUDIT_SLOT_STATUS_TIMEOUT) -> tuple[int | None, int | None]:
    """Best-effort query of the local proxy slot-status endpoint.

    Returns ``(available_slots, total_slots)`` from
    ``/llama/local/status``, or ``(None, None)`` when the endpoint is
    unreachable, times out, returns non-JSON, or lacks the slot fields
    (fail-open — the caller degrades to the configured static ceiling).

    The query uses a short timeout (``AUDIT_SLOT_STATUS_TIMEOUT`` = 1 s) and
    never raises: the dynamic ceiling must never block or fail the audit
    when the endpoint is unavailable (LP-0MSQ32S2M001EA74 AC3).
    """
    target = url or os.environ.get(AUDIT_SLOT_STATUS_URL_ENV, AUDIT_SLOT_STATUS_URL_DEFAULT)
    try:
        with urllib.request.urlopen(target, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        available = data.get("available_slots")
        total = data.get("total_slots")
        if available is None or total is None:
            return None, None
        return int(available), int(total)
    except Exception:  # noqa: BLE001 — best-effort, fail-open
        return None, None


def _query_proxy_mode(base_url: str | None = None,
                      timeout: float = AUDIT_PROXY_MODE_TIMEOUT) -> str | None:
    """Best-effort query of the llm-manager proxy operating mode.

    GETs ``<base>/admin/mode`` and returns the ``mode`` field (e.g.
    ``"fast"`` / ``"cheap"``), or ``None`` when the endpoint is unreachable,
    times out, returns non-JSON, a non-200 status, or lacks the ``mode``
    field (fail-open — the caller leaves parallelism settings unchanged).

    Read-only by mandate: this function never issues ``POST /admin/set-mode``
    (mode switching is operator/herdr territory; the audit mandate stays
    read-only, SA-0MSN04X2S006ONH0).

    The query uses a short timeout (``AUDIT_PROXY_MODE_TIMEOUT`` = 3 s) and
    never raises: the mode check must never block or fail the audit.
    """
    target = base_url or os.environ.get(
        AUDIT_PROXY_BASE_URL_ENV, AUDIT_PROXY_BASE_URL_DEFAULT
    )
    endpoint = target.rstrip("/") + "/admin/mode"
    try:
        with urllib.request.urlopen(endpoint, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
        mode = data.get("mode")
        return mode if isinstance(mode, str) else None
    except Exception:  # noqa: BLE001 — best-effort, fail-open
        return None


def _apply_proxy_mode_serialization() -> None:
    """Serialize parallelism when the proxy is in ``cheap`` mode.

    Called once at runner start (before any pi call). When the proxy reports
    mode exactly ``"cheap"`` (case-sensitive), this run's pi launches are
    capped to one at a time by setting both ``AUDIT_PARALLELISM=1`` and
    ``AUDIT_MAX_CONCURRENCY=1`` in this process's environment — so the
    audit does not race the proxy's single-slot cheap-mode pool.

    Fail-open: any other mode (including ``"fast"``) or a failed query
    (unreachable / timeout / non-200 / unparseable) leaves all parallelism
    settings unchanged; a warning is logged to stderr only on query
    failure. The change is per-process (``os.environ``) — it affects only
    this run's spawned pi subprocesses, never other processes or audits.
    """
    mode = _query_proxy_mode()
    if mode == "cheap":
        os.environ[AUDIT_PARALLELISM_ENV] = "1"
        os.environ[ENV_MAX_WORKERS] = "1"
        print(
            "Proxy mode is 'cheap' — serializing audit pi calls "
            "(AUDIT_PARALLELISM=1, AUDIT_MAX_CONCURRENCY=1).",
            file=sys.stderr,
        )
    elif mode is None:
        print(
            "Warning: could not determine proxy mode (fail-open — "
            "parallelism settings unchanged).",
            file=sys.stderr,
        )
    # Any other reported mode (e.g. "fast") → unchanged, no log noise.


def _resolve_max_child_concurrency() -> int:
    """Resolve the max child-call concurrency cap (static floor).

    Precedence:
      1. ``AUDIT_MAX_CHILD_CONCURRENCY`` environment variable (integer >= 1)
      2. ``_resolve_parallelism()`` (``AUDIT_PARALLELISM`` env or 2)

    Values below 1 are clamped to 1. An invalid (non-integer) value is
    ignored with a warning so a misconfigured environment cannot break the
    audit run.
    """
    env_value = os.environ.get(AUDIT_MAX_CHILD_CONCURRENCY_ENV)
    if env_value:
        try:
            parsed = int(env_value)
            return max(1, parsed)
        except ValueError:
            print(
                f"Warning: invalid {AUDIT_MAX_CHILD_CONCURRENCY_ENV} value {env_value!r}; "
                "using default max child concurrency",
                file=sys.stderr,
            )
    return _resolve_parallelism()


def _resolve_child_concurrency() -> int:
    """Resolve the slot-aware dynamic child-call concurrency ceiling.

    Queries the local proxy status endpoint for ``available_slots`` /
    ``total_slots`` and derives the child-call ceiling from the available
    headroom: ``min(max(1, available_headroom), configured_max)`` where
    ``available_headroom = total_slots - slots_in_use`` (i.e. the free
    slots reported by the endpoint).

    - When the slot query fails (unreachable/timeout/error), the runner
      degrades gracefully to the configured static ceiling
      (``AUDIT_MAX_CHILD_CONCURRENCY`` > ``AUDIT_PARALLELISM`` > 2)
      — the dynamic ceiling never blocks or fails the audit (fail-open).
    - The result is always at least 1 (never 0) so a saturated pool
      degrades to sequential execution rather than stalling the audit.

    The dynamic ceiling is queried once per child-call batch dispatch
    (not per call) to avoid oscillation (LP-0MSQ32S2M001EA74 AC3).
    """
    configured_max = _resolve_max_child_concurrency()
    available, total = _query_slot_status()
    if available is None or total is None:
        return configured_max
    # Available headroom = free slots reported by the endpoint
    # (available_slots; slots_in_use = total - available).
    headroom = max(0, int(available))
    return max(1, min(headroom, configured_max))


AUDIT_PHASE2_BATCH_ENV = "AUDIT_PHASE2_BATCH"
"""Environment variable enabling Phase 2 batch deep analysis (P6).

When set to a truthy value (and ``--batch-phase2`` is not passed), Phase 2
folds the parent ACs and each pending child's ACs into ONE indexed pi call
(``phase2_batch``), falling back to the per-child path on failure. This
eliminates the N+1 Phase 2 calls for parents with many children.
"""


def _phase2_batch_enabled(cli_value: bool | None = None) -> bool:
    """Resolve whether Phase 2 batch deep analysis is enabled.

    Precedence:
      1. ``--batch-phase2`` CLI flag (explicit override)
      2. ``AUDIT_PHASE2_BATCH`` environment variable (truthy)
      3. ``False`` (default — preserves the existing per-child call pattern)
    """
    if cli_value is not None:
        return cli_value
    env_value = os.environ.get(AUDIT_PHASE2_BATCH_ENV, "")
    return env_value.strip().lower() in ("1", "true", "yes", "on")


def _normalize_model_source(source: str | None) -> str:
    """Normalize a model_source value to a valid value (remote|local)."""
    if not source:
        return DEFAULT_MODEL_SOURCE
    normalized = str(source).strip().lower()
    if normalized in MODEL_SOURCES:
        return normalized
    return DEFAULT_MODEL_SOURCE


def _coerce_model_str(value: object) -> str | None:
    """Extract a non-empty trimmed string from *value*, or None."""
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed:
            return trimmed
    return None


def _resolve_phase_model_value(value: object, model_source: str) -> str | None:
    """Resolve a model value that may be a string or source-mapped dict.

    If *value* is a plain string, return it directly.
    If *value* is a dict with keys matching *model_source* (remote|local),
    return the corresponding value.
    """
    direct = _coerce_model_str(value)
    if direct:
        return direct
    if isinstance(value, dict):
        source_value = _coerce_model_str(value.get(model_source))
        if source_value:
            return source_value
    return None


def _extract_phase_model_config(config: dict) -> dict[str, object]:
    """Extract per-phase model config from the loaded .ralph.json.

    Checks these locations (in order):
      - model.<phase>  (nested key)
      - model.remote.<phase> / model.local.<phase>  (source-mapped)
      - model[phase]   (dict access)
      - model[remote|local][phase]  (source-mapped dict access)
    """
    phase_config: dict[str, object] = {}
    model_root = config.get("model")

    for phase in (AUDIT_PHASE,):
        # Check dotted keys first (model.audit, model.remote.audit, etc.)
        dotted_key = config.get(f"model.{phase}")
        if dotted_key is not None:
            phase_config[phase] = dotted_key
            continue

        direct_remote = config.get(f"model.remote.{phase}")
        direct_local = config.get(f"model.local.{phase}")
        if direct_remote is not None or direct_local is not None:
            source_map: dict[str, object] = {}
            if direct_remote is not None:
                source_map["remote"] = direct_remote
            if direct_local is not None:
                source_map["local"] = direct_local
            phase_config[phase] = source_map
            continue

        if isinstance(model_root, dict):
            if phase in model_root:
                phase_config[phase] = model_root[phase]
                continue

            remote_map = model_root.get("remote")
            local_map = model_root.get("local")
            if isinstance(remote_map, dict) or isinstance(local_map, dict):
                source_map = {}
                if isinstance(remote_map, dict) and phase in remote_map:
                    source_map["remote"] = remote_map[phase]
                if isinstance(local_map, dict) and phase in local_map:
                    source_map["local"] = local_map[phase]
                if source_map:
                    phase_config[phase] = source_map

    return phase_config


def _resolve_model_for_phase(phase: str, config: dict,
                              model_source: str,
                              cli_model: str | None = None) -> str:
    """Resolve the model for *phase* with the resolution chain:

    1. --model CLI flag (explicit override, highest priority)
    2. Config-driven: phase model from .ralph.json resolved via model_source
    3. Hardcoded fallback: DEFAULT_MODEL

    This mirrors Ralph's _resolve_model_for_phase pattern.
    """
    # 1. CLI override
    explicit = _coerce_model_str(cli_model)
    if explicit:
        return explicit

    # 2. Config-driven resolution
    phase_config = _extract_phase_model_config(config)
    config_value = phase_config.get(phase)
    resolved = _resolve_phase_model_value(config_value, model_source)
    if resolved:
        return resolved

    # 3. Hardcoded fallback
    return DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Pi integration (duplicated from ralph for now – see OQ-1)
# ---------------------------------------------------------------------------

def _resolve_call_timeout(timeout: int | None, child_screen: bool = False) -> int:
    """Resolve the effective per-call Pi timeout for a single call.

    Precedence:
      1. explicit ``timeout`` arg (from ``--timeout`` / ``AUDIT_PI_TIMEOUT``)
      2. child Phase-1 AC-review screens: ``AUDIT_CHILD_SCREEN_TIMEOUT``
         env or ``_CHILD_SCREEN_TIMEOUT_DEFAULT`` (600 s) — a lightweight
         child screen that exceeds its short budget fails fast instead of
         burning the full 1800 s (LP-0MSQ32S2M001EA74 AC1)
      3. all other calls: ``CALL_PI_TIMEOUT`` (1800 s)
    """
    if timeout is not None:
        return timeout
    if child_screen:
        return _resolve_child_screen_timeout(None) or _CHILD_SCREEN_TIMEOUT_DEFAULT
    return CALL_PI_TIMEOUT


def _communicate_with_stall(process, cmd: list[str],
                            effective_timeout: int,
                            stall_timeout: int) -> tuple[str, str]:
    """Communicate with the pi process, aborting early on stall.

    Incrementally reads stdout/stderr (no pipe-buffer deadlocks) and tracks
    the last output-arrival time. When no output arrives for
    ``stall_timeout`` seconds, raises ``subprocess.TimeoutExpired`` with
    ``timeout=stall_timeout`` so the caller can kill the process and emit a
    stall-abort verdict (LP-0MSQ32S2M001EA74 AC2). When the total
    ``effective_timeout`` budget expires first, raises
    ``subprocess.TimeoutExpired`` with ``timeout=effective_timeout``.

    Returns ``(stdout, stderr)`` on normal completion. Falls back to the
    historical blocking ``communicate(timeout=...)`` when the process does
    not expose select-able stdout/stderr streams (e.g. mocked processes in
    unit tests), preserving the existing contract for those callers.
    """
    # Mocked processes (unit tests) expose MagicMock streams without real
    # fds; a real Popen with text=True exposes TextIOWrapper objects whose
    # fileno() is an int. Fall back to blocking communicate() for the mock
    # path so existing callers/tests are unchanged.
    try:
        out_fd = process.stdout.fileno()
        err_fd = process.stderr.fileno()
        if not (isinstance(out_fd, int) and isinstance(err_fd, int)):
            return process.communicate(timeout=effective_timeout)
    except (AttributeError, OSError, ValueError):
        return process.communicate(timeout=effective_timeout)

    deadline = time.monotonic() + effective_timeout
    last_output = time.monotonic()
    out_chunks: list[str] = []
    err_chunks: list[str] = []
    fds = {out_fd: (process.stdout, out_chunks), err_fd: (process.stderr, err_chunks)}
    while fds:
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            raise subprocess.TimeoutExpired(cmd, effective_timeout)
        stall_remaining = stall_timeout - (now - last_output)
        if stall_remaining <= 0:
            raise subprocess.TimeoutExpired(cmd, stall_timeout)
        wait = min(remaining, stall_remaining)
        try:
            rlist, _, _ = select.select(list(fds.keys()), [], [], max(wait, 0.01))
        except (OSError, ValueError):
            break  # pipes closed / process gone
        if process.poll() is not None and not rlist:
            break
        for fd in rlist:
            _stream, chunks = fds[fd]
            try:
                data = os.read(fd, 65536)
            except OSError:
                del fds[fd]
                continue
            if not data:
                # EOF on this pipe: stop watching it (select keeps reporting
                # EOF'd fds as readable, so we must drop it to avoid a busy loop).
                del fds[fd]
                continue
            last_output = time.monotonic()
            chunks.append(data.decode(errors="replace"))
    # Drain any remaining buffered output after the process exits
    for fd, (_stream, chunks) in ((out_fd, (process.stdout, out_chunks)),
                                  (err_fd, (process.stderr, err_chunks))):
        try:
            while True:
                data = os.read(fd, 65536)
                if not data:
                    break
                chunks.append(data.decode(errors="replace"))
        except OSError:
            break
    return "".join(out_chunks), "".join(err_chunks)


def _call_pi(prompt: str, model: str = DEFAULT_MODEL,
             pi_bin: str = "pi",
             enable_tools: bool = False,
             timeout: int | None = None,
             max_retries: int | None = None,
             ac_fallback_used: threading.Event | None = None,
             child_screen: bool = False) -> dict:
    """Call Pi via subprocess and parse the JSON-stream response.

    Args:
        prompt: The prompt text to send to Pi.
        model: The model name to use.
        pi_bin: Path to the pi binary.
        enable_tools: If True, adds ``--tools read,bash,grep,find,ls
                       --exclude-tools ask_question`` to enable file-reading
                       capabilities in the Pi agent session.

        # Context reduction: every call adds ``--no-context-files --no-skills``
        so each pi session starts with minimal static context (~2KB instead of
        ~49KB of duplicated global+project AGENTS.md plus the skills section).
        Audit prompts are fully self-contained (read-only mandate, JSON
        format, FILE SCOPE manifest, criteria) and must never depend on
        AGENTS.md or skill descriptions.

        max_retries: Maximum number of extra attempts after a provider error.
            When None, falls back to ``_PI_MAX_RETRIES`` (2). Long
            agent-mode Phase 2 calls pass 1 so a provider error does not
            restart a long call multiple times (evaluation lever P5).

        child_screen: If True, this is a lightweight child Phase-1 AC-review
            screen; it uses the short per-call budget
            (``_CHILD_SCREEN_TIMEOUT_DEFAULT`` = 600 s, configurable via
            ``AUDIT_CHILD_SCREEN_TIMEOUT`` / ``--child-screen-timeout``)
            instead of the 1800 s Phase-2 budget (LP-0MSQ32S2M001EA74 AC1).

    Returns a dict with keys ``verdict`` and ``evidence``.
    On success, implementations may also include additional diagnostic keys
    such as ``raw_stdout``, ``raw_stderr`` and ``extracted_text`` which are
    useful for debugging. When the pi JSON stream reported provider usage,
    the dict also carries ``input_tokens`` (initial input-token count for
    the call, from the ``agent_end`` message's usage block) and
    ``elapsed_seconds`` (wall-clock duration incl. retries) — both feed the
    per-call timing line and the context-reduction verification
    (SA-0MSISKM8F004NW1U AC2). This function returns at minimum
    ``{"verdict": <met|unmet|partial|adjusted>, "evidence": <text>}``.

    Uses the same JSON-stream protocol as ralph (``pi -p --mode json``).
    Uses ``communicate()`` to avoid pipe-buffer deadlocks. In-process
    stall detection (LP-0MSQ32S2M001EA74 AC2) aborts a call that produces
    no output for ``AUDIT_STALL_TIMEOUT`` seconds (default 600) well
    before the full per-call budget expires.
    """
    cmd = [pi_bin, "-p", "--mode", "json", "--model", model, prompt]
    if enable_tools:
        cmd.extend([
            "--tools", "read,bash,grep,find,ls",
            "--exclude-tools", "ask_question",
        ])
    # Context reduction (SA-0MSISKM8F004NW1U): audit prompts are fully
    # self-contained, so drop the duplicated global+project AGENTS.md load
    # (~40KB) and the skills section (~7KB) from every pi call in both tool
    # modes. Both flags are loader toggles compatible with --mode json and
    # --tools; prompts must never rely on AGENTS.md or skill descriptions.
    cmd.extend(["--no-context-files", "--no-skills"])

    effective_timeout = _resolve_call_timeout(timeout, child_screen=child_screen)
    stall_timeout = _resolve_stall_timeout()
    effective_max_retries = _PI_MAX_RETRIES if max_retries is None else max_retries
    attempt = 0
    provider_error: str | None = None
    stdout = ""
    stderr = ""
    # Wall-clock baseline for per-call timing instrumentation. Measures the
    # full call including any provider-error retries so operators can see the
    # true per-call duration in the Phase 2 performance baseline.
    _call_start = time.monotonic()
    while True:
        attempt += 1
        try:
            # Concurrency cap: bound concurrent pi subprocesses host-wide
            # (fan-out investigation SA-0MSAEKOQE009TEB4). Each pi launch
            # holds one audit slot; the wait is AUDIT_LOCK_TIMEOUT (default
            # 0s = fail fast when the ceiling is saturated).
            with _acquire_audit_slot():
                try:
                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                    )
                except FileNotFoundError:
                    raise RuntimeError(f"pi binary not found: {pi_bin}")

                try:
                    stdout, stderr = _communicate_with_stall(
                        process, cmd, effective_timeout, stall_timeout,
                    )
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    stdout, stderr = process.communicate()
                    if ac_fallback_used is not None:
                        ac_fallback_used.set()
                    stalled = exc.timeout == stall_timeout
                    if stalled:
                        evidence = (
                            f"Pi model call stalled (no output for {stall_timeout}s); "
                            "aborted early. Manual audit required."
                        )
                    else:
                        evidence = (
                            f"Pi model call timed out after {effective_timeout}s. "
                            "Manual audit required."
                        )
                    return {
                        "verdict": "unmet",
                        "evidence": evidence,
                        "raw_stdout": stdout,
                        "raw_stderr": stderr,
                        "extracted_text": "",
                        "_timeout": True,
                        "elapsed_seconds": time.monotonic() - _call_start,
                    }
        except TimeoutError as exc:
            # Ceiling saturated past the bounded wait: do not launch yet
            # another unbounded pi process. Report a clear unmet verdict so
            # the audit completes gracefully and the operator can retry.
            if ac_fallback_used is not None:
                ac_fallback_used.set()
            return {
                "verdict": "unmet",
                "evidence": (
                    f"Audit concurrency limit reached: {exc}. "
                    "Retry when fewer audits are running."
                ),
                "raw_stdout": "",
                "raw_stderr": "",
                "extracted_text": "",
                "_concurrency_timeout": True,
                "elapsed_seconds": time.monotonic() - _call_start,
            }

        # Detect provider errors (e.g. "finish_reason: error" where the model
        # never emits its final structured output) and retry them with backoff.
        provider_error = _extract_provider_error(stdout or "")
        if provider_error is None or attempt > effective_max_retries:
            break
        time.sleep(_PI_RETRY_BACKOFF_SECONDS * attempt)

    elapsed_seconds = time.monotonic() - _call_start

    if provider_error:
        if ac_fallback_used is not None:
            ac_fallback_used.set()
        return {
            "verdict": "unmet",
            "evidence": f"Pi provider error: {provider_error}",
            "raw_stdout": stdout,
            "raw_stderr": stderr,
            "extracted_text": "",
            "_provider_error": True,
            "_provider_error_message": provider_error,
            "elapsed_seconds": elapsed_seconds,
        }

    raw = stdout or ""
    if not raw:
        return {"verdict": "unmet", "evidence": "", "raw_stdout": stdout, "raw_stderr": stderr, "elapsed_seconds": elapsed_seconds}

    # Parse JSON lines looking for the final agent_end message
    text = extract_pi_text(raw)
    if not text:
        return {"verdict": "unmet", "evidence": "", "raw_stdout": stdout, "raw_stderr": stderr, "elapsed_seconds": elapsed_seconds}

    # Input-token capture (AC2 of SA-0MSISKM8F004NW1U): the pi JSON stream's
    # agent_end message carries the provider usage block, so each call's
    # initial context size (static context + prompt) is measurable. This
    # lets operators verify the context-reduction bound (<10K input tokens
    # per audit session) from the per-call timing line alone.
    input_tokens = _extract_input_tokens(raw)

    # Try to parse the text as JSON with verdict/evidence
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return {
                "verdict": _normalize_verdict(obj.get("verdict", "unmet")),
                "evidence": obj.get("evidence", ""),
                "raw_stdout": stdout,
                "raw_stderr": stderr,
                "extracted_text": text,
                "elapsed_seconds": elapsed_seconds,
                "input_tokens": input_tokens,
            }
    except json.JSONDecodeError:
        pass

    # If Pi returned free-form text, use it as evidence and default to met
    return {"verdict": "met", "evidence": text.strip()[:200], "raw_stdout": stdout, "raw_stderr": stderr, "extracted_text": text, "elapsed_seconds": elapsed_seconds, "input_tokens": input_tokens}


def _extract_input_tokens(raw: str) -> int | None:
    """Extract the initial input-token count from a pi ``--mode json`` stream.

    pi emits an ``agent_end`` event whose ``messages`` array contains the
    final assistant message with a ``usage`` block, e.g.
    ``{"input": 769, "output": 10, "totalTokens": 779}``. ``input`` is the
    total prompt tokens for that call (static context + user prompt) and is
    used to verify the context-reduction bound (SA-0MSISKM8F004NW1U AC2:
    fewer than 10K input tokens per audit session). Returns None when the
    stream has no usable usage data.
    """
    if not raw:
        return None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "agent_end":
            continue
        messages = obj.get("messages")
        if not isinstance(messages, list):
            return None
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            usage = msg.get("usage")
            if isinstance(usage, dict):
                input_tokens = usage.get("input")
                if isinstance(input_tokens, int) and input_tokens >= 0:
                    return input_tokens
        return None
    return None


def _extract_provider_error(raw: str) -> str | None:
    """Return the provider error message if the Pi stream ended in a provider error.

    Scans the JSON stream for ``agent_end`` events and inspects the last
    assistant message for ``stopReason == "error"`` or an ``errorMessage``
    field (as seen with Local Proxy ``finish_reason: error`` failures where
    the model never emits its final structured output).

    Returns the error message, or ``None`` if no provider error is present.
    """
    if not raw:
        return None
    provider_error: str | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "agent_end":
            continue
        messages = obj.get("messages")
        if not isinstance(messages, list):
            continue
        for msg in reversed(messages):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            stop_reason = msg.get("stopReason")
            error_message = msg.get("errorMessage")
            if stop_reason == "error" or error_message:
                provider_error = error_message or f"provider stop reason: {stop_reason}"
            break
    return provider_error


# ---------------------------------------------------------------------------
# Acceptance-criteria extractor
# ---------------------------------------------------------------------------

def _extract_json_array(text: str) -> list | None:
    """Extract the last JSON array from text that may contain analysis before the array.

    Pi often returns analysis text followed by a JSON array at the end.
    This function finds the last `[` that is NOT inside a string and tries to parse.

    Returns the parsed list if found, otherwise None.
    """
    if not text:
        return None

    # Find positions of `[` that could start a JSON array
    # We need to skip `[` characters that are inside JSON strings
    possible_starts = []
    in_string = False
    escape_next = False

    for i, char in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if char == '\\' and in_string:
            escape_next = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        # We're not in a string
        if char == '[':
            # Check if this could be the start of a JSON array
            rest = text[i + 1:].lstrip()
            if rest and (rest[0] in ('{', '"', ']', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '-')):
                possible_starts.append(i)

    # Try each possible start position from last to first
    for start in reversed(possible_starts):
        candidate = text[start:].strip()

        # Try the full candidate first
        try:
            result = json.loads(candidate)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

        # If that failed, try to find where the JSON array ends
        for end_search in range(len(candidate) - 1, 0, -1):
            if candidate[end_search] == ']':
                try:
                    result = json.loads(candidate[:end_search + 1])
                    if isinstance(result, list):
                        return result
                except json.JSONDecodeError:
                    continue

    return None


_CHECKBOX_MARKER_RE = re.compile(r"^\[[ xX~-]\]\s*")
"""Checkbox marker prefix stripped from bullet acceptance criteria.

Matches ``[ ]``, ``[x]``, ``[X]``, ``[~]`` and ``[-]`` markers. Numbered ACs
are left untouched (see _extract_acs).
"""


def _extract_json_object(text: str, required_keys: Sequence[str] = ()) -> dict | None:
    """Extract a JSON object from text that may contain analysis before it.

    Mirrors ``_extract_json_array`` for object-shaped responses (e.g. the
    project-level summary/recommendation). Pi often returns analysis text
    followed by a JSON object at the end; this scans candidates from the last
    ``{`` backwards so the trailing object wins.

    When *required_keys* is non-empty, a candidate is returned only if it
    contains every required key with a non-empty string value; otherwise the
    scan continues so a nested fragment never shadows the real response (e.g.
    ``{"summary": "...", "meta": {...}, "recommendation": "..."}`` resolves
    to the outer object). If no candidate satisfies the required keys, the
    last parsed object is returned as a fallback.

    Returns the best parsed dict, otherwise None.
    """
    if not text:
        return None

    # Find positions of `{` that could start a JSON object, skipping `{`
    # characters that are inside JSON strings.
    possible_starts = []
    in_string = False
    escape_next = False

    for i, char in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if char == '\\' and in_string:
            escape_next = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == '{':
            possible_starts.append(i)

    fallback: dict | None = None
    # Try each possible start position from last to first
    for start in reversed(possible_starts):
        candidate = text[start:].strip()
        parsed = _parse_json_object_prefix(candidate)
        if parsed is None:
            continue
        if fallback is None:
            fallback = parsed
        if required_keys:
            if all(
                isinstance(parsed.get(key), str) and parsed.get(key).strip()
                for key in required_keys
            ):
                return parsed
        else:
            return parsed

    return fallback


def _parse_json_object_prefix(text: str) -> dict | None:
    """Parse *text* as a JSON object, tolerating trailing content.

    First tries the full *text*; if that fails, finds the end of the
    outermost JSON object and parses just that prefix (this tolerates
    markdown fences or trailing prose after the object). Returns the dict or
    None.
    """
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Find where the JSON object ends, skipping nested braces and strings.
    depth = 0
    in_str = False
    esc = False
    for end_search, char in enumerate(text):
        if esc:
            esc = False
            continue
        if char == '\\' and in_str:
            esc = True
            continue
        if char == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                try:
                    result = json.loads(text[:end_search + 1])
                    if isinstance(result, dict):
                        return result
                except json.JSONDecodeError:
                    pass
                break

    return None


def _strip_checkbox(text: str) -> str:
    """Strip a leading markdown checkbox marker from *text* (bullets only)."""
    return _CHECKBOX_MARKER_RE.sub("", text, count=1)


def _normalize_heading_line(line: str) -> str:
    """Strip markdown heading / bold / angle-bracket markers from *line* so
    heading variants all match the core Acceptance / Success Criteria
    pattern (SA-0MSJLC8XA00178YD).
    """
    line = line.strip()
    line = re.sub(r"^#{0,3}\s*", "", line)
    line = line.replace("**", "")
    line = re.sub(r"<<([^>]*)>>", r"\1", line)
    return line.strip()


def _extract_acs(description: str) -> list[str]:
    """Extract acceptance criteria lines from a markdown description."""
    heading_re = re.compile(
        r"^(?:Acceptance|Success)\s+Criteria:?\s*(?:\([^)]*\))?\s*$",
        re.IGNORECASE,
    )
    lines = description.splitlines()
    heading_idx = None
    for idx, raw in enumerate(lines):
        if heading_re.search(_normalize_heading_line(raw)):
            heading_idx = idx
            break
    if heading_idx is None:
        return ["No acceptance criteria defined."]

    acs: list[str] = []
    for line in lines[heading_idx + 1 :]:
        stripped = line.strip()
        if re.match(r"^#{1,6}\s", stripped):
            break
        numbered = re.match(r"^\d+\.\s+(.*)", stripped)
        if numbered:
            acs.append(numbered.group(1))
            continue
        bulleted = re.match(r"^[-*]\s+(.*)", stripped)
        if bulleted:
            acs.append(_strip_checkbox(bulleted.group(1)))
            continue
        if acs and stripped:
            if line[:1].isspace():
                # Indented continuation line: fold into the current bullet.
                acs[-1] = f"{acs[-1]} {stripped}"
                continue
            break

    if not acs:
        return ["No acceptance criteria defined."]
    return acs


# ---------------------------------------------------------------------------
# Phase 2 file-scope manifest (SA-0MSAIXI1E005SZPV)
# ---------------------------------------------------------------------------

_FILE_SCOPE_MAX_FILES = 50
"""Maximum number of files to include in the Phase 2 file-scope manifest."""

_FILE_SCOPE_MAX_INDEX = 25
"""Maximum number of repo-index entries in the Phase 2 file-scope manifest."""


def _extract_key_files(description: str) -> list[str]:
    """Extract file paths from the work item's Key Files section.

    Scans *description* for a markdown heading like ``## Key Files`` or
    ``## Key Files (predicted)`` and returns the backtick-quoted paths listed
    beneath it (one per bullet/numbered line). Returns an empty list when no
    Key Files section is present.
    """
    if not description:
        return []
    pattern = re.compile(
        r"^#{0,4}\s*Key Files.*?$",
        re.MULTILINE | re.IGNORECASE,
    )
    match = pattern.search(description)
    if not match:
        return []
    files: list[str] = []
    for line in description[match.end():].splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^#{1,6}\s", stripped):
            break  # next heading ends the Key Files section
        # Match a backtick-quoted path, e.g. ``- `path/to/file.py` — desc``
        m = re.search(r"`([^`]+)`", stripped)
        if m:
            candidate = m.group(1).strip()
        else:
            # Fall back to the first whitespace-delimited token on a bullet line
            bullet = re.match(r"^[-*]\s+(\S+)", stripped)
            candidate = bullet.group(1) if bullet else ""
        if candidate:
            files.append(candidate)
    return files


def _git_changed_files(runner: Runner) -> list[str]:
    """Return the list of changed/untracked files from git (bounded).

    Combines ``git diff --name-only HEAD`` and ``git status --porcelain=v1``
    so both tracked modifications and untracked files are captured. Any git
    failure returns an empty list so the audit never breaks on VCS errors.
    """
    changed: list[str] = []
    try:
        proc = runner(["git", "diff", "--name-only", "HEAD"])
        if proc.returncode == 0 and proc.stdout:
            changed.extend(
                ln.strip() for ln in proc.stdout.splitlines() if ln.strip()
            )
    except Exception:  # noqa: S110, BLE001 -- git is best-effort for the manifest
        pass
    try:
        proc = runner(["git", "status", "--porcelain=v1"])
        if proc.returncode == 0 and proc.stdout:
            for ln in proc.stdout.splitlines():
                # porcelain format: "XY path" (2 status chars + space + path)
                if len(ln) >= 4:
                    path = ln[3:].strip()
                    if path and path not in changed:
                        changed.append(path)
    except Exception:  # noqa: S110, BLE001 -- git is best-effort for the manifest
        pass
    return changed[:_FILE_SCOPE_MAX_FILES]


def _repo_index(runner: Runner, max_entries: int = _FILE_SCOPE_MAX_INDEX) -> list[str]:
    """Return a lightweight repo index (top-level entries with file counts).

    Uses ``git ls-files`` to count files per top-level path and returns the
    ``max_entries`` largest buckets as ``path/ (N files)`` strings. On git
    failure, falls back to a best-effort directory listing of
    ``TARGET_PROJECT_ROOT``.
    """
    buckets: dict[str, int] = {}
    try:
        proc = runner(["git", "ls-files"])
        if proc.returncode == 0 and proc.stdout:
            for ln in proc.stdout.splitlines():
                rel = ln.strip()
                if not rel:
                    continue
                top = rel.split("/", 1)[0] if "/" in rel else "(root)"
                buckets[top] = buckets.get(top, 0) + 1
    except Exception:  # noqa: S110, BLE001 -- git is best-effort for the manifest
        pass

    if not buckets:
        # Best-effort fallback: list top-level dirs of TARGET_PROJECT_ROOT
        try:
            root = TARGET_PROJECT_ROOT
            for entry in sorted(root.iterdir()):
                if entry.is_dir() and not entry.name.startswith("."):
                    buckets[entry.name] = len(list(entry.iterdir()))
        except OSError:
            return []

    ordered = sorted(buckets.items(), key=lambda kv: -kv[1])[:max_entries]
    return [f"{name}/ ({count} files)" for name, count in ordered]


def _phase1_evidence_refs(ac_results: list[dict],
                          max_refs: int = _FILE_SCOPE_MAX_FILES) -> list[str]:
    """Extract file:line references from Phase 1 evidence (P4).

    Scans each AC result's ``evidence`` for ``path:line`` patterns and
    returns them (bounded) so Phase 2 verifies named files rather than
    re-exploring the repo.
    """
    refs: list[str] = []
    seen: set[str] = set()
    for r in ac_results:
        evidence = r.get("evidence", "") or ""
        for m in re.finditer(r"([\w./-]+\.\w+):(\d+)", evidence):
            ref = f"{m.group(1)}:{m.group(2)}"
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
        if len(refs) >= max_refs:
            break
    return refs


def _build_file_scope_manifest(issue: dict, ac_results: list[dict],
                               runner: Runner | None = None) -> str:
    """Build the file-scope manifest injected into the Phase 2 prompt.

    Combines the work item's Key Files, the git changed-file list, a
    lightweight repo index, and Phase 1 evidence file:line refs (P4). The
    manifest lets the model verify in-scope files without unbounded
    repository exploration (the dominant Phase 2 cost).

    *runner* is used for git queries and defaults to ``_default_runner``.
    """
    if runner is None:
        runner = _default_runner

    sections: list[str] = []

    key_files = _extract_key_files(issue.get("description", ""))
    if key_files:
        key_lines = "\n".join(f"- `{f}`" for f in key_files[:_FILE_SCOPE_MAX_FILES])
        sections.append(f"Key Files (from the work item):\n{key_lines}")

    changed = _git_changed_files(runner)
    if changed:
        changed_lines = "\n".join(f"- `{f}`" for f in changed)
        sections.append(f"Changed files (git diff / status):\n{changed_lines}")

    refs = _phase1_evidence_refs(ac_results)
    if refs:
        ref_lines = "\n".join(f"- `{f}`" for f in refs)
        sections.append(f"Phase 1 evidence file:line references:\n{ref_lines}")

    index = _repo_index(runner)
    if index:
        index_lines = "\n".join(f"- {f}" for f in index)
        sections.append(f"Repository index (top-level layout):\n{index_lines}")

    if not sections:
        return "No file scope manifest available — verify criteria against the most relevant implementation files."
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

# Sentinel to detect when model/model_source were not explicitly passed
_MISSING = object()


def _assemble_issue_report(issue: dict, ac_results: list[dict],
                           child_results: list[dict],
                           code_quality_findings: list[dict] | None = None,
                           code_quality_fixes_applied: int = 0,
                           code_quality_skipped_reason: str | None = None,
                           fp_screen_results: list[dict] | None = None,
                           remediation_results: dict | None = None,
                           model: str | None = _MISSING,
                           model_source: str | None = _MISSING,
                           phase2_completed: bool = False,
                           phase2_skip_note: str | None = None,
                           green_run_sha: str | None = None,
                           auto_green_run_sha: str | None = None,
                           test_skill_run_sha: str | None = None,
                           content_fingerprint: str | None = None) -> str:
    """Assemble the canonical issue-mode audit report.

    *ac_results* is a list of ``{"text": ..., "verdict": ..., "evidence": ...}``.
    *child_results* is a list of child review dicts with keys:
      ``title``, ``id``, ``status``, ``stage``, ``ac_results``.
    *code_quality_findings* is an optional list of finding dicts from
      code quality checks. Each dict has ``severity``, ``file``, ``line``,
      ``message``, ``linter``, ``code`` keys.
    *code_quality_skipped_reason* is an optional string explaining why
      code quality was not run (e.g., no linters available).
    *fp_screen_results* is the optional list of false-positive screen
      entries (see ``_parse_fp_screen_response``). When provided, the Code
      Quality section lists each screened finding's classification +
      justification, and only critical/high findings NOT classified
      ``confident-false-positive`` block closure (SA-0MST01O4G002VPBR AC4).
    *remediation_results* is the optional dict from ``_run_remediation_loop``
      (T2/F2). When the loop ran (iterations > 0 or exhaustion), the Code
      Quality section gains a ``#### Remediation loop`` subsection listing
      the applied config-fix commits, the fingerprint re-hash, and the
      cap-exhaustion outcome.
    *model* is the name of the model used for the audit (e.g.,
      ``"opencode-go/deepseek-v4-flash"``). When not provided, no model
      line is emitted (backward compatibility). When provided as ``None``
      or empty string, the fallback ``Model: manual (no provider)`` is used.
    *model_source* is the source of the model (``"local"`` or ``"remote"``).
      When provided alongside *model*, produces
      ``Model: <model> (provider: <source>)``.
    *green_run_sha* is the operator-attested green-run commit sha (when a
      valid ``--green-run``/``AUDIT_GREEN_RUN`` attestation was accepted).
      When provided, a ``Green run attestation: <sha>`` line is emitted near
      the ``Ready to close`` header so the report records the external
      evidence the execution-dependent ACs were marked met on.

    *auto_green_run_sha* is the automatically-verified green-run commit sha
      (when a green full-suite run was found read-only in the per-repo test
      cache, SA-0MSIU5HFI0024D7W). When provided, an
      ``Automatic green run evidence: <sha> (cached full-suite run)`` line is
      emitted near the header so the report records the evidence source.

    *test_skill_run_sha* is the executed full-suite commit sha (when the
      ``--run-tests`` path executed the suite via the test skill and it
      passed, SA-0MSJELSWS002UF60). When provided, a
      ``Test skill run evidence: <sha> (executed full-suite run, --run-tests)``
      line is emitted near the header so the report records the evidence
      source distinctly from the cache-consumption path.

    *content_fingerprint* is the content fingerprint (git HEAD sha +
      description hash + Key Files, see ``_compute_content_fingerprint``)
      captured at audit time. When provided, an
      ``Audit content fingerprint: <hex>`` line is emitted near the header so
      the persisted report carries the freshness gate data
      (SA-0MSKB6US1009CNHT). The line is parsed back by
      ``_extract_content_fingerprint``.

    Ready-to-close logic:
      - All acceptance criteria (parent + children) must be ``met`` or ``adjusted``.
        ``adjusted`` criteria represent acceptable variance and do not block closure.
      - All non-deleted children must be in ``in_review`` or ``done`` stage.
        Children with ``status: in_progress`` but ``stage: in_review`` are
        acceptable and do NOT block closure.
      - Children in ``in_review`` or ``done`` stage are exempt from child
        audit verdict checks. Per the audit spec, children in ``in_review``
        do NOT block closure — only pre-review stages (``idea``,
        ``intake_complete``, ``plan_complete``) block.
      - Code quality findings: critical or high severity findings block closure
        ("Ready to close: No"). Medium and low findings produce warnings
        but do NOT block closure.
    """
    all_ac_acceptable = all(
        r["verdict"] in _ACCEPTABLE_VERDICTS
        for r in ac_results + [c for cr in child_results for c in cr.get("ac_results", [])]
    )
    # Check that all active children are in in_review or done stage.
    # Children that inherited the parent's pass (parent-first pass-through,
    # SA-0MSKB6VJA005N43F) count as reviewed by virtue of the parent — they
    # are not independently audited but the parent verdict covers them.
    active_children = [c for c in child_results if c.get("stage") not in ("", None)]
    all_children_reviewed = all(
        c.get("stage") in ("in_review", "done") or c.get("inherited_pass")
        for c in active_children
    )

    # Check each active (non-exempt) child's persisted audit verdict
    # Exempt children: status=deleted (wl delete), completed/done (already closed),
    # and those in in_review stage (per spec, in_review children do not
    # block parent closure — only pre-review stages block).
    def _is_exempt_child(c: dict) -> bool:
        # Deleted children are fully closed
        if c.get("status") == "deleted":
            return True
        # Completed/done children are fully closed
        if c.get("status") == "completed" and c.get("stage") == "done":
            return True
        # Children in in_review stage should not have their audit verdicts
        # block parent closure (per audit spec)
        return c.get("stage") == "in_review"

    non_exempt_children = [c for c in active_children if not _is_exempt_child(c)]
    any_child_audit_not_ready = any(
        c.get("child_audit_ready") is False
        for c in non_exempt_children
    )

    # Code quality blocking: critical or high findings block closure
    # unless the false-positive screen classified them
    # confident-false-positive (uncertain findings stay blocking).
    cq_findings = code_quality_findings or []
    has_blocking_cq = bool(_effective_blocking_findings(
        cq_findings, fp_screen_results or []
    ))

    ready_before_cq = "Yes" if (all_ac_acceptable and all_children_reviewed and not any_child_audit_not_ready) else "No"
    if ready_before_cq == "Yes" and has_blocking_cq:
        ready = "No"
    else:
        ready = ready_before_cq

    # Build model line (only when model/model_source was explicitly provided)
    issue_id_label = issue.get("id", "") or "unknown"
    if model is not _MISSING:
        effective_model = (model or "").strip() or "manual"
        effective_source = ((model_source or "") if model_source is not _MISSING else "").strip()
        if effective_source:
            model_line = f"Model: {effective_model} (provider: {effective_source})"
        else:
            model_line = f"Model: {effective_model} (no provider)"
        lines = [
            f"Ready to close: {ready}",
            "",
            f"Audit report for work item {issue_id_label}",
            "",
            model_line,
        ]
        if green_run_sha:
            lines.append("")
            lines.append(f"Green run attestation: {green_run_sha}")
        if auto_green_run_sha:
            lines.append("")
            lines.append(
                f"Automatic green run evidence: {auto_green_run_sha} "
                "(cached full-suite run)"
            )
        if test_skill_run_sha:
            lines.append("")
            lines.append(
                f"Test skill run evidence: {test_skill_run_sha} "
                "(executed full-suite run, --run-tests)"
            )
        if content_fingerprint:
            lines.append("")
            lines.append(f"{AUDIT_CONTENT_FINGERPRINT_PREFIX}{content_fingerprint}")
        lines.extend(["", "## Summary", ""])
    else:
        lines = [
            f"Ready to close: {ready}",
            "",
            f"Audit report for work item {issue_id_label}",
        ]
        if green_run_sha:
            lines.append("")
            lines.append(f"Green run attestation: {green_run_sha}")
        if auto_green_run_sha:
            lines.append("")
            lines.append(
                f"Automatic green run evidence: {auto_green_run_sha} "
                "(cached full-suite run)"
            )
        if test_skill_run_sha:
            lines.append("")
            lines.append(
                f"Test skill run evidence: {test_skill_run_sha} "
                "(executed full-suite run, --run-tests)"
            )
        if content_fingerprint:
            lines.append("")
            lines.append(f"{AUDIT_CONTENT_FINGERPRINT_PREFIX}{content_fingerprint}")
        lines.extend(["", "## Summary", ""])

    # Count verdicts across all criteria (parent + children)
    all_criteria = ac_results + [c for cr in child_results for c in cr.get("ac_results", [])]
    _met_count = sum(1 for r in all_criteria if r["verdict"] == VERDICT_MET)
    adjusted_count = sum(1 for r in all_criteria if r["verdict"] == VERDICT_ADJUSTED)
    unmet_count = sum(1 for r in all_criteria if r["verdict"] == VERDICT_UNMET)
    partial_count = sum(1 for r in all_criteria if r["verdict"] == VERDICT_PARTIAL)

    not_reviewed = [
        c for c in child_results
        if c.get("stage") not in ("in_review", "done", "")
    ]

    if ready_before_cq == "Yes":
        if has_blocking_cq:
            lines.append(
                "All acceptance criteria are met and children are reviewed, "
                "but code quality findings block closure."
            )
        else:
            parts = []
            parts.append(
                f"All {len(ac_results)} acceptance criteria for work item "
                f"{issue.get('id', '?')} are acceptable"
            )
            if adjusted_count > 0:
                parts.append(f"({adjusted_count} with acceptable variance)")
            parts.append(". All children are in in_review or done stage.")
            if phase2_skip_note:
                parts.append(f" Phase 2 deep analysis skipped: {phase2_skip_note}.")
            elif phase2_completed:
                parts.append(" Deep code analysis (Phase 2) completed and confirmed all verdicts.")
            lines.append(" ".join(parts))
    else:
        if not phase2_completed and any(
            r["verdict"] == VERDICT_PARTIAL
            and "pending deep code review" in r.get("evidence", "")
            for r in ac_results
        ):
            lines.append(
                "Phase 1 automated screening detected blocking issues. "
                "All 'met' verdicts have been demoted to 'partial' (pending deep code review). "
                "Phase 2 deep analysis was skipped. Resolve Phase 1 blockers and re-audit."
            )
        elif unmet_count > 0 and not_reviewed:
            lines.append(
                f"{unmet_count} acceptance criteria not met AND "
                f"{len(not_reviewed)} children not yet in in_review/done stage."
            )
        elif unmet_count > 0:
            lines.append(
                f"{unmet_count} of {len(ac_results)} acceptance criteria for "
                f"work item {issue.get('id', '?')} are not met."
            )
        elif partial_count > 0:
            lines.append(
                f"{partial_count} of {len(ac_results)} acceptance criteria are "
                f"only partially met."
            )
        else:
            lines.append(
                f"{len(not_reviewed)} children not yet in in_review/done stage."
            )

    lines.append("")
    lines.append("## Acceptance Criteria Status")
    lines.append("")
    lines.append("| # | Criterion | Verdict | Evidence |")
    lines.append("|---|-----------|---------|----------|")

    if ac_results and ac_results[0].get("text") == "No acceptance criteria defined.":
        lines.append("")
        lines.append("No acceptance criteria defined.")
    else:
        for i, r in enumerate(ac_results, 1):
            evidence = r.get("evidence", "") or ""
            lines.append(
                f"| {i} | {r['text']} | {r['verdict']} | {evidence} |"
            )

    # Variance Decisions section: appears when any parent or child criterion
    # has 'adjusted' verdict
    variance_criteria = [
        {"index": i + 1, "source": "parent", "text": r["text"], "evidence": r.get("evidence", "") or ""}
        for i, r in enumerate(ac_results)
        if r["verdict"] == VERDICT_ADJUSTED
    ]
    for child in child_results:
        for i, r in enumerate(child.get("ac_results", [])):
            if r["verdict"] == VERDICT_ADJUSTED:
                variance_criteria.append({
                    "index": i + 1,
                    "source": f"child ({child.get('id', '')})",
                    "text": r["text"],
                    "evidence": r.get("evidence", "") or "",
                })

    if variance_criteria:
        lines.append("")
        lines.append("## Variance Decisions")
        lines.append("")
        lines.append("The following acceptance criteria have acceptable variance."
                      " These criteria were adjusted during implementation but"
                      " satisfy the user story intent.")
        lines.append("")
        lines.append("| # | Source | Criterion | Justification |")
        lines.append("|---|--------|-----------|---------------|")
        for vc in variance_criteria:
            lines.append(
                f"| {vc['index']} | {vc['source']} | {vc['text']} | {vc['evidence']} |"
            )

    lines.append("")
    lines.append("## Children Status")
    lines.append("")

    if not child_results:
        lines.append("No children.")
    else:
        capped = len(child_results) > _CHILDREN_CAP
        reviewed = child_results[:_CHILDREN_CAP]
        for child in reviewed:
            lines.append(
                f"### {child['title']} ({child['id']}) — "
                f"{child['status']}/{child['stage']}"
            )
            lines.append("")
            if child.get("reused_from"):
                # Child-verdict reuse (LP-0MSQ32MF200675AR): the child's
                # fresh valid audit (content fingerprint unchanged + verdict
                # present) was reused — no fresh audit was performed, so the
                # verdict table comes from the child's own report.
                lines.append(
                    f"**Child verdict reused from {child['reused_from']} — "
                    "content unchanged, no fresh audit performed.**"
                )
                lines.append("")
            if child.get("inherited_pass"):
                # Parent-first pass-through (SA-0MSKB6VJA005N43F): the child
                # inherited the parent's pass — explicit, never silent.
                lines.append(
                    "**Inherited from parent pass** — the parent audit found no "
                    "gaps, so this child was not independently audited."
                )
                lines.append("")
            elif child.get("pass_through") == "unrelated_to_gaps":
                lines.append(
                    "**Not audited (unrelated to parent gaps)** — the parent "
                    "audit has gaps in other areas; this child is not mapped to "
                    "any gap and was not audited."
                )
                lines.append("")
            if child.get("ac_results"):
                lines.append("| # | Criterion | Verdict | Evidence |")
                lines.append("|---|-----------|---------|----------|")
                for i, r in enumerate(child["ac_results"], 1):
                    evidence = r.get("evidence", "") or ""
                    lines.append(
                        f"| {i} | {r['text']} | {r['verdict']} | {evidence} |"
                    )
            else:
                lines.append("No acceptance criteria defined.")
            lines.append("")

        if capped:
            remaining = len(child_results) - _CHILDREN_CAP
            lines.append(
                f"*{_CHILDREN_CAP} children reviewed; {remaining} omitted for brevity.*"
            )

    lines.append("")
    lines.append("### Code Quality")
    lines.append("")

    cq_findings = code_quality_findings or []
    cq_fixes = code_quality_fixes_applied
    fp_results = fp_screen_results or []
    rem = remediation_results or {}

    if code_quality_skipped_reason:
        lines.append(f"Code quality check skipped: {code_quality_skipped_reason}")
    elif not cq_findings and cq_fixes == 0:
        lines.append("No code quality issues found.")
    elif not cq_findings and cq_fixes > 0:
        lines.append(f"All issues auto-fixed by **{cq_fixes}** linter(s).")
        lines.append("No remaining issues.")
    else:
        effective_blocking = _effective_blocking_findings(cq_findings, fp_results)
        if effective_blocking:
            lines.append(
                "**Critical and/or high severity findings detected — "
                "these block closure.**"
            )
        elif any(
            f.get("severity") in ("critical", "high") for f in cq_findings
        ):
            lines.append(
                "**Critical/high severity findings were screened as "
                "confident false positives — they no longer block closure.**"
            )
        else:
            lines.append(
                "**Medium/low severity findings detected — "
                "these are reported as warnings and do not block closure.**"
            )
        lines.append("")
        lines.append("| # | Severity | File | Line | Message | Linter | Code |")
        lines.append("|---|----------|------|------|---------|--------|------|")
        for i, f in enumerate(cq_findings, 1):
            lines.append(
                f"| {i} | {f.get('severity', '?')} | "
                f"{f.get('file', '?')} | {f.get('line', 0)} | "
                f"{f.get('message', '')} | {f.get('linter', '?')} | "
                f"{f.get('code', '')} |"
            )

        # False-positive screen section (SA-0MST01O4G002VPBR AC3/AC4):
        # per-finding classifications + justifications surface in the
        # human-readable report. Screen-failed runs annotate every entry.
        if fp_results:
            lines.append("")
            lines.append("#### False-positive screen")
            lines.append("")
            any_failed = any(e.get("screen_failed") for e in fp_results)
            if any_failed:
                lines.append(
                    "**Screen degraded (provider error / timeout / unparseable "
                    "output) — all findings defaulted to uncertain (caution-first).**"
                )
                lines.append("")
            lines.append("| # | File | Line | Code | Classification | Justification |")
            lines.append("|---|------|------|------|----------------|---------------|")
            for e in fp_results:
                f = e.get("finding", {})
                lines.append(
                    f"| {e.get('index', 0) + 1} | {f.get('file', '?')} | "
                    f"{f.get('line', 0)} | {f.get('code', '?')} | "
                    f"{e.get('classification', '?')} | "
                    f"{e.get('justification', '')} |"
                )

    # Remediation loop section (SA-0MST01OIN008MXYT / F2): applied
    # config-fix commits + fingerprint re-hash + cap-exhaustion outcome.
    # Rendered outside the findings branch so a successful remediation
    # (which clears the findings) still surfaces the loop record.
    if (rem.get("iterations") or rem.get("exhausted")
            or rem.get("chore_items") or rem.get("chore_failures")):
        lines.append("")
        lines.append("#### Remediation loop")
        lines.append("")
        lines.append(
            f"Config-fix iterations: {rem.get('iterations', 0)} / "
            f"{rem.get('max_iterations', REMEDIATION_MAX_ITERATIONS_DEFAULT)}"
        )
        if rem.get("exhausted"):
            lines.append(
                f"**Cap exhausted — persisting findings remain blocking "
                f"'genuine' ({REMEDIATION_EXHAUSTED_ANNOTATION}).**"
            )
        for c in rem.get("commits", []):
            fp_after = c.get("fingerprint_after") or "unavailable"
            change = c.get("change") or "per-file-ignores"
            lines.append(
                f"- Commit {c.get('sha') or 'n/a'} ({c.get('file', '?')}) "
                f"— {change}; fingerprint re-hashed after commit: {fp_after}"
            )
        for ci in rem.get("chore_items", []):
            commit_ref = ci.get("commit_sha")
            if commit_ref:
                lines.append(
                    f"- Chore work item {ci.get('id', '?')} tracks the "
                    f"config fix (commit {commit_ref})"
                )
            else:
                lines.append(
                    f"- Chore work item {ci.get('id', '?')} tracks a "
                    f"medium/low false positive — {FP_CHORE_ANNOTATION}"
                )
        for f in rem.get("chore_failures", []):
            ref = (f.get("change")
                   or (f.get("finding", {}).get("code", "?") if isinstance(
                       f.get("finding"), dict) else "?"))
            lines.append(
                f"- ⚠ Chore tracking failed ({f.get('error', '')}) — "
                f"{ref}; the commit stands, the finding stays blocking "
                f"'genuine'"
            )

    lines.append("")
    return "\n".join(lines)


def _assemble_child_audit_report(child: dict, ac_results: list[dict],
                                 model: str | None = _MISSING,
                                 model_source: str | None = _MISSING,
                                 content_fingerprint: str | None = None) -> str:
    """Assemble an audit report for a single child work item.

    *child* is a dict with keys ``title``, ``id``, ``status``, ``stage``.
    *ac_results* is a list of ``{"text": ..., "verdict": ..., "evidence": ...}``.
    *model* is the name of the model used for the audit. When not provided,
      no model line is emitted. When ``None`` or empty, the fallback
      ``Model: manual (no provider)`` is used.
    *model_source* is the source of the model (``"local"`` or ``"remote"``).
    *content_fingerprint* is the content fingerprint (git HEAD sha +
      description hash + Key Files, see ``_compute_content_fingerprint``)
      captured at audit time. When provided, an
      ``Audit content fingerprint: <hex>`` line is emitted near the header so
      the persisted child report stays content-gate-able on future parent
      runs (LP-0MSQ32MF200675AR).

    Ready-to-close logic:
      - All acceptance criteria must be ``met`` or ``adjusted``.
        ``adjusted`` criteria represent acceptable variance and do not block closure.
    """
    all_acceptable = all(r["verdict"] in _ACCEPTABLE_VERDICTS for r in ac_results) if ac_results else False
    ready = "Yes" if all_acceptable else "No"

    lines = [
        f"Ready to close: {ready}",
        "",
    ]

    # Build model line (only when model was explicitly provided)
    if model is not _MISSING:
        effective_model = (model or "").strip() or "manual"
        effective_source = ((model_source or "") if model_source is not _MISSING else "").strip()
        if effective_source:
            lines.append(f"Model: {effective_model} (provider: {effective_source})")
        else:
            lines.append(f"Model: {effective_model} (no provider)")
        lines.append("")

    if content_fingerprint:
        lines.append(f"{AUDIT_CONTENT_FINGERPRINT_PREFIX}{content_fingerprint}")
        lines.append("")

    lines.extend([
        "## Summary",
        "",
        (f"Child work item audit for {child['title']} ({child['id']}). "
        f"Status: {child['status']}/{child['stage']}."),
        "",
        "## Acceptance Criteria Status",
        "",
        "| # | Criterion | Verdict | Evidence |",
        "|---|-----------|---------|----------|",
    ])

    if not ac_results:
        lines.append("")
        lines.append("No acceptance criteria defined.")
    else:
        for i, r in enumerate(ac_results, 1):
            evidence = r.get("evidence", "") or ""
            lines.append(
                f"| {i} | {r['text']} | {r['verdict']} | {evidence} |"
            )

    # Variance Decisions section for child report
    variance_criteria = [
        {"index": i + 1, "text": r["text"], "evidence": r.get("evidence", "") or ""}
        for i, r in enumerate(ac_results)
        if r["verdict"] == VERDICT_ADJUSTED
    ]
    if variance_criteria:
        lines.append("")
        lines.append("## Variance Decisions")
        lines.append("")
        lines.append("The following acceptance criteria have acceptable variance:")
        lines.append("")
        lines.append("| # | Criterion | Justification |")
        lines.append("|---|-----------|---------------|")
        for vc in variance_criteria:
            lines.append(
                f"| {vc['index']} | {vc['text']} | {vc['evidence']} |"
            )

    lines.append("")
    return "\n".join(lines)


def _persist_child_audit(
    child_id: str,
    child_title: str,
    child_status: str,
    child_stage: str,
    ac_results: list[dict],
    pi_bin: str = "pi",
    model: str | None = None,
    model_source: str | None = None,
    worklog_dir: str | None = None,
    content_fingerprint: str | None = None,
) -> tuple[bool, str]:
    """Assemble and persist an audit report for a single child work item.

    *model* and *model_source* are passed through to
    ``_assemble_child_audit_report()`` for inclusion in the child report.
    *worklog_dir* is forwarded to ``persist_audit`` so the wl invocation
    targets the correct worklog store regardless of the caller's cwd.
    *content_fingerprint* (when provided) is embedded in the child report so
    parent-persisted child audits stay content-gate-able on future runs
    (LP-0MSQ32MF200675AR).

    Returns (rc, report_text).
    rc == 0 on success; rc == PERSIST_CONTENT_INVALID (4) when only the
    compact fallback notice was persisted (usable, identity/readback guards
    pass); any other non-zero rc means NOTHING was persisted — callers abort
    the parent run rather than emit a misleading parent report whose child
    audits never landed (LP-0MSQ32HNR007AI6B).
    On failure the report text is still returned so callers can log it.
    """
    child = {
        "title": child_title,
        "id": child_id,
        "status": child_status,
        "stage": child_stage,
    }
    report = _assemble_child_audit_report(
        child, ac_results, model=model, model_source=model_source,
        content_fingerprint=content_fingerprint,
    )

    rc = persist_audit(child_id, report, worklog_dir=worklog_dir)
    return rc, report


def _assemble_project_report(summary: str, recommendation: str) -> str:
    """Assemble the canonical project-mode audit report."""
    lines = [
        "Ready to close: No",
        "",
        "## Summary",
        "",
        summary,
        "",
        "## Recommendation",
        "",
        recommendation,
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Debug / debug-log helpers
# ---------------------------------------------------------------------------

def _debug_log_dir() -> Path:
    """Return the debug-log scratch directory (outside .worklog/ and repo).

    Defaults to ``~/.audit_debug/<project-slug>/`` so debug files never sit in
    scanned paths (the 9.5 GB .worklog audit_debug dump was a scan trap).
    The directory is created lazily by callers via ``_write_debug_log``.
    """
    home = Path.home()
    slug = "".join(c if (c.isalnum() or c in "-_") else "-" for c in TARGET_PROJECT_ROOT.name)
    return home / ".audit_debug" / (slug or "project")


def _default_debug_log_path(issue_id: str, context: str) -> Path:
    """Return a sensible default path for debug logs.

    Tests monkeypatch this helper so callers should use it rather than
    hard-coding a path. Debug logs are transient forensics: they live under
    ``~/.audit_debug/<project>/`` (outside ``.worklog/`` and outside the repo
    tree) so recursive greps never walk them, and are swept by
    ``cleanup_debug_logs.py``.
    """
    p = _debug_log_dir() / f"audit_debug_{issue_id}.jsonl"
    return p


def _write_debug_log(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _remove_debug_log(debug_log: str | None, issue_id: str, *contexts: str) -> None:
    """Remove a run's debug log file after a successful audit run.

    Debug logs are transient forensics: a successful run leaves nothing
    behind (including explicit ``--debug-log`` runs). A failed run keeps the
    file for forensics — the caller only invokes this on success. Any
    deletion error is swallowed so cleanup can never mask the main result.

    Args:
        debug_log: explicit ``--debug-log`` path (if given, only it is removed).
        issue_id: work item id for the default-path lookup.
        *contexts: contexts the run may have written under (default-path only).
    """
    try:
        if debug_log:
            target = Path(debug_log)
        else:
            candidates = [
                _default_debug_log_path(issue_id, ctx) for ctx in (contexts or ("parent",))
            ]
            target = candidates[0] if len(set(candidates)) == 1 else None
            if target is None:
                # Multiple distinct default paths: remove each.
                for cand in set(candidates):
                    if cand.exists():
                        cand.unlink()
                return
        if target.exists():
            target.unlink()
    except OSError:
        pass  # Cleanup must never mask the audit result


def _call_pi_and_maybe_log(issue_id: str, context: str, prompt: str,
                           model: str = DEFAULT_MODEL,
                           pi_bin: str = "pi",
                           debug_log: str | None = None,
                           enable_tools: bool = False,
                           timeout: int | None = None,
                           max_retries: int | None = None,
                           ac_fallback_used: threading.Event | None = None,
                           child_screen: bool = False,
                           ac_count: int | None = None) -> dict:
    """Call _call_pi and optionally write debug information to a log.

    Args:
        issue_id: Work item ID for debug log naming.
        context: Context string for debug log naming.
        prompt: The prompt text to send to Pi.
        model: The model name to use.
        pi_bin: Path to the pi binary.
        debug_log: Optional path for debug log output.
        enable_tools: If True, forwards to _call_pi() to enable file-reading
                       tools in the Pi agent session.
        max_retries: Maximum extra provider-error attempts; forwarded to
            _call_pi(). Phase 2 deep analysis passes 1 (see
            ``_PHASE2_MAX_RETRIES``) so long agent-mode calls are not
            restarted multiple times on a provider error.
        child_screen: If True, forwards to _call_pi() so child Phase-1
            AC-review screens use the short per-call budget
            (LP-0MSQ32S2M001EA74 AC1).
        ac_count: Number of acceptance criteria covered by this call. When
            supplied, the per-call timing line appends ``ac_count=N`` and
            ``avg_ac_elapsed_seconds`` (elapsed / N) so Phase-2 per-AC
            latency is visible and regressions surface (LP-0MSQ32WM5000NCB7
            F4 AC1). A count of 0 is emitted without the avg field; a
            missing count keeps the legacy timing format byte-for-byte.

        # Context reduction: every forwarded pi call runs with
        ``--no-context-files --no-skills`` (see _call_pi) so audit sessions
        start with minimal static context; prompts are self-contained.

    If *debug_log* is provided the entry reason will be "debug_log" and the
    provided path will be used. If *debug_log* is not provided but the pi
    result contains diagnostic fields (``raw_stdout``/``raw_stderr``), a
    default path from ``_default_debug_log_path`` will be used and the reason
    will be "parse_failure".
    """
    result = _call_pi(prompt, model=model, pi_bin=pi_bin, enable_tools=enable_tools, timeout=timeout, max_retries=max_retries, ac_fallback_used=ac_fallback_used, child_screen=child_screen)

    # Emit a per-call timing line to stderr (performance baseline). Includes
    # issue id, call context, and elapsed seconds so Phase 2 durations are
    # measurable and regressions are visible without a debug log. When the pi
    # stream reported usage, the initial input-token count is appended so the
    # context-reduction bound (<10K tokens, SA-0MSISKM8F004NW1U AC2) is
    # verifiable from the timing line alone. Phase-2 call sites additionally
    # pass an AC count so the line surfaces per-AC latency
    # (``ac_count`` + ``avg_ac_elapsed_seconds``, LP-0MSQ32WM5000NCB7 F4).
    elapsed = result.get("elapsed_seconds")
    if elapsed is not None:
        timing = (
            f"Per-call timing: issue_id={issue_id} context={context} "
            f"elapsed_seconds={float(elapsed):.2f}"
        )
        input_tokens = result.get("input_tokens")
        if input_tokens is not None:
            timing += f" input_tokens={input_tokens}"
        if ac_count is not None:
            timing += f" ac_count={ac_count}"
            if ac_count > 0:
                timing += (
                    f" avg_ac_elapsed_seconds={float(elapsed) / ac_count:.2f}"
                )
        print(timing, file=sys.stderr)

    # Decide whether to write a debug line
    reason = None
    target = None
    if debug_log:
        reason = "debug_log"
        target = Path(debug_log)
    elif isinstance(result, dict) and (result.get("raw_stdout") or result.get("raw_stderr")):
        reason = "provider_error" if result.get("_provider_error") else "parse_failure"
        target = _default_debug_log_path(issue_id, context)

    if reason and target:
        entry = {
            "issue_id": issue_id,
            "context": context,
            "reason": reason,
            "raw_stdout": result.get("raw_stdout"),
            "raw_stderr": result.get("raw_stderr"),
            "extracted_text": result.get("extracted_text"),
            "evidence": result.get("evidence"),
            "provider_error": result.get("_provider_error_message"),
            "elapsed_seconds": result.get("elapsed_seconds"),
            "input_tokens": result.get("input_tokens"),
            "prompt": prompt[:1000],
        }
        try:
            _write_debug_log(target, entry)
        except Exception:  # noqa: S110, BLE001 -- debug logging must not break audit execution
            pass

    return result


# ---------------------------------------------------------------------------
# Subcommand: issue
# ---------------------------------------------------------------------------

def _demote_met_to_partial(results: list[dict]) -> list[dict]:
    """Demote any 'met' verdicts to 'partial' with a pending deep review note.

    Used when Phase 1 (automated screening) detects blocking issues,
    preventing Phase 2 (deep code analysis) from running.
    """
    demoted: list[dict] = []
    for r in results:
        if r["verdict"] == VERDICT_MET:
            demoted.append({
                "text": r["text"],
                "verdict": VERDICT_PARTIAL,
                "evidence": "pending deep code review (Phase 1 blocked)",
            })
        else:
            demoted.append(dict(r))
    return demoted


def _get_child_audit_verdict(runner: Runner, child_id: str,
                             worklog_dir: str | None = None,
                             force: bool = False,
                             child: dict | None = None) -> tuple[bool | None, str, str | None]:
    """Check a child's persisted audit verdict via wl audit-show.

    Returns a (verdict, reason, audited_at) tuple:
        (True, "ready", audited_at)      — Child audit says "Ready to close: Yes"
        (False, "not_ready", audited_at) — Child audit says "Ready to close: No"
        (None, "no_audit", None)         — No audit data found (audit-show returned null/empty)
        (None, "stale", audited_at)      — Audit exists but is stale (content changed / time gate)
        (None, "force", audited_at)      — --force bypassed reuse (LP-0MSQ32MF200675AR)
        (None, "error", None)            — wl audit-show command failed

    Freshness (LP-0MSQ32MF200675AR) uses the CONTENT-fingerprint gate
    FIRST — same logic as the item-level gate (_check_audit_freshness,
    SA-0MSKB6US1009CNHT): when the stored report carries a content
    fingerprint (git HEAD sha + description hash + Key Files captured at
    audit time), the audit is fresh iff the fingerprint is unchanged, so a
    child whose updatedAt moved for non-content reasons (comments, status
    bumps) is reused instead of re-audited. The legacy TIME gate
    (auditedAt vs updatedAt + AUDIT_FRESHNESS_BUFFER_SECONDS) is kept as
    the floor for fingerprint-less legacy reports. When *force* is set,
    BOTH gates are bypassed and every child is re-audited.

    The child's auditedAt is returned so callers can record the reuse
    marker ("reused from <auditedAt>") in the parent report.

    *child* may be passed in to avoid a redundant ``wl show`` when the
    caller already fetched the child via ``wl show --children``
    (SA-0MSL1Z7E9005TLBA): it supplies the description for the fingerprint
    computation and the updatedAt for the legacy time gate. When omitted,
    the child is fetched via ``wl show``.
    """

    try:
        data = _run_wl(runner, ["wl", "audit-show", child_id, "--json"],
                       worklog_dir=worklog_dir)
    except RuntimeError:
        return None, "error", None

    if not isinstance(data, dict) or data.get("success") is False:
        return None, "error", None

    audit = data.get("audit")
    if not audit:
        return None, "no_audit", None

    raw_output = audit.get("rawOutput")
    if not raw_output:
        return None, "no_audit", None

    audited_at = audit.get("auditedAt")
    if not audited_at:
        return None, "no_audit", None

    if force:
        # --force on the parent bypasses child reuse (LP-0MSQ32MF200675AR
        # AC4): every child is re-audited, even one with a fresh stored
        # audit — no content gate, no time gate.
        return None, "force", audited_at

    # ── Content-fingerprint gate (primary, LP-0MSQ32MF200675AR) ─────────
    # Same freshness logic as the item-level gate: when the stored report
    # carries a content fingerprint, freshness is decided by content match
    # (unchanged = fresh). This is what lets a parent reuse children that
    # were individually audited (in_review, all ACs acceptable) instead of
    # re-running Phase 1 + Phase 2 for them — the child-audit cost driver.
    stored_fingerprint = _extract_content_fingerprint(raw_output)
    if stored_fingerprint is not None:
        current_fingerprint = _compute_content_fingerprint(
            runner, child_id, worklog_dir=worklog_dir, work_item=child,
        )
        if current_fingerprint is None:
            # Fingerprint cannot be computed now (e.g. git unavailable) —
            # fail open and let the child pipeline re-run.
            return None, "stale", audited_at
        if current_fingerprint != stored_fingerprint:
            # Content changed → stale → re-audit.
            return None, "stale", audited_at
        # Fingerprint unchanged + verdict present → fresh.
        return _parse_child_audit_verdict(raw_output, audited_at)

    # ── Time gate (floor): legacy audits without a fingerprint ──────────
    # Get the work item's updatedAt (reuse an already-fetched child dict
    # when the caller has it — SA-0MSL1Z7E9005TLBA).
    if child is not None:
        updated_at = child.get("updatedAt")
    else:
        try:
            wi_data = _run_wl(runner, ["wl", "show", child_id, "--json"],
                              worklog_dir=worklog_dir)
        except RuntimeError:
            # Can't check freshness; treat as fresh since we have an audit
            updated_at = None
        else:
            work_item = wi_data.get("workItem", {}) if isinstance(wi_data, dict) else {}
            updated_at = work_item.get("updatedAt")
    if updated_at:
        audit_time = _parse_iso_utc(audited_at)
        update_time = _parse_iso_utc(updated_at)
        if (audit_time is not None and update_time is not None
                and not _audit_time_is_fresh(audit_time, update_time)):
            # The child's own persistence write bumps its updatedAt to
            # ~the audit's auditedAt. When the two timestamps coincide
            # within the persistence tolerance, the bump is the audit's
            # own write, so the audit reflects current state and is
            # trusted (prevents the parent runner from re-triggering
            # child audits forever — SA-0MSI3XH34001LLU4).
            write_delta = update_time - audit_time
            if not (timedelta(0) <= write_delta
                    <= timedelta(seconds=AUDIT_PERSIST_WRITE_TOLERANCE_SECONDS)):
                return None, "stale", audited_at

    return _parse_child_audit_verdict(raw_output, audited_at)


def _parse_child_audit_verdict(raw_output: str,
                               audited_at: str) -> tuple[bool | None, str, str | None]:
    """Parse the ``Ready to close:`` verdict from a child audit report.

    Returns ``(True, "ready", audited_at)`` / ``(False, "not_ready",
    audited_at)`` when a verdict line is found, otherwise
    ``(None, "no_audit", audited_at)``.
    """
    ready = _parse_ready_to_close(raw_output)
    if ready is None:
        return None, "no_audit", audited_at
    if ready == "yes":
        return True, "ready", audited_at
    return False, "not_ready", audited_at


def _fetch_child_audited_at(runner: Runner, child_id: str,
                            worklog_dir: str | None = None) -> str | None:
    """Return the child audit's ``auditedAt`` (None when unavailable).

    Used by the auto-trigger loop's content-freshness reuse branch to record
    the ``reused from <auditedAt>`` marker (LP-0MSQ32MF200675AR). The branch
    is a defense-in-depth fallback — the primary child reuse happens in the
    Phase 1 pre-pass where ``_get_child_audit_verdict`` already returns the
    auditedAt.
    """
    try:
        data = _run_wl(runner, ["wl", "audit-show", child_id, "--json"],
                       worklog_dir=worklog_dir)
    except RuntimeError:
        return None
    audit = data.get("audit") if isinstance(data, dict) else None
    if not audit:
        return None
    return audit.get("auditedAt")


# Matches the canonical audit report AC table row:
#   | 1 | criterion text | met | evidence text |
# The criterion/evidence fields are lazy (may contain pipes, rarely).
_AUDIT_AC_TABLE_ROW = re.compile(
    r"^\|\s*(\d+)\s*\|\s*(.*?)\s*\|\s*(met|unmet|partial|adjusted)\s*\|\s*(.*?)\s*\|$"
)


def _parse_audit_report_acs(raw_output: str) -> list[dict] | None:
    """Parse the Acceptance Criteria Status table from a persisted audit report.

    Both the issue-level and child-level audit reports use the canonical
    ``| # | Criterion | Verdict | Evidence |`` table (see
    ``_assemble_issue_report`` / ``_assemble_child_audit_report``). Returns a
    list of ``{"text", "verdict", "evidence"}`` dicts, or ``None`` when no
    parseable rows are found.
    """
    rows: list[dict] = []
    for line in raw_output.splitlines():
        match = _AUDIT_AC_TABLE_ROW.match(line.strip())
        if not match:
            continue
        rows.append({
            "text": match.group(2).strip(),
            "verdict": match.group(3).strip(),
            "evidence": match.group(4).strip(),
        })
    return rows if rows else None


def _child_acs_from_own_audit(child: dict, runner: Runner,
                              worklog_dir: str | None = None,
                              fallback: list[dict] | None = None) -> list[dict]:
    """Reuse the AC verdicts persisted in a child's own fresh audit (P7/P12).

    Called for children whose persisted audit verdict is ``ready``
    (``child_audit_ready=True``) so the Phase 1 child AC review call can be
    skipped, and for children whose own fresh audit returned ``not ready``
    (P12) so the parent's Phase 2 skips the duplicated deep-analysis call.
    Parses the child's own audit report table for per-AC verdicts; if the
    table cannot be parsed, falls back to the provided *fallback* (e.g. the
    child's existing Phase 1 screening results) when given, otherwise marks
    each extracted AC as ``met`` with a reuse note (a fresh ready audit means
    all ACs were deemed acceptable). Never raises: any failure falls back to
    the same fallback chain.
    """
    child_desc = child.get("description", "")
    child_acs = _extract_acs(child_desc)
    fallback_met = [
        {
            "text": ac,
            "verdict": VERDICT_MET,
            "evidence": (
                "Verified by the child's own fresh audit "
                "(child_audit_ready=True); Phase 1 screening call skipped."
            ),
        }
        for ac in child_acs
        if ac != "No acceptance criteria defined."
    ]

    try:
        data = _run_wl(runner, ["wl", "audit-show", child.get("id", ""), "--json"],
                       worklog_dir=worklog_dir)
    except RuntimeError:
        return fallback if fallback is not None else fallback_met
    if not isinstance(data, dict):
        return fallback if fallback is not None else fallback_met
    audit = data.get("audit")
    if not audit:
        return fallback if fallback is not None else fallback_met
    raw_output = audit.get("rawOutput")
    if not raw_output:
        return fallback if fallback is not None else fallback_met
    parsed = _parse_audit_report_acs(raw_output)
    if parsed:
        return parsed
    return fallback if fallback is not None else fallback_met


def _phase1_review_child_acs(ci: int, child: dict, resolved_model: str,
                             pi_bin: str, debug_log: str | None,
                             timeout: int | None, runner: Runner,
                             script_failure_callback: Callable[[str, Exception], None],
                             ac_fallback_used: threading.Event | None = None
                             ) -> tuple[int, list[dict]]:
    """Phase 1 child AC review worker (P7, parallel-safe).

    Runs the batched Phase 1 acceptance-criteria screening for one child and
    returns ``(ci, child_ac_results)``. The prompt includes the file-scope
    manifest and SCANNING block, and the call runs with read-only tools
    (``enable_tools=True``) — mirroring the Phase 2 performance pattern.

    Never raises: a Pi ``RuntimeError`` records a script failure and falls
    back to diagnostic ``partial`` verdicts (identical to the historical
    sequential Phase 1 child path).
    """
    child_desc = child.get("description", "")
    child_acs = _extract_acs(child_desc)
    child_ac_results: list[dict] = []
    if child_acs and child_acs[0] != "No acceptance criteria defined.":
        child_ac_list = json.dumps([
            {"index": i, "text": ac} for i, ac in enumerate(child_acs)
        ])
        file_scope = _build_file_scope_manifest(child, [], runner=runner)
        prompt = (
            f"[READ-ONLY AUDIT] You are performing a read-only audit. "
            f"Do NOT close, modify, create, or delete any work items. "
            f"Do NOT execute any wl, git, or other state-modifying commands. "
            f"Return ONLY a structured JSON array.\n\n"
            f"Review the following acceptance criteria for child work item '{child.get('title', '')}' "
            f"against the codebase. "
            f"Return ONLY a JSON array of objects, each with keys 'index' (integer), "
            f"'verdict' (one of: met, unmet, partial, adjusted) and 'evidence' "
            f"(a one-line note with file:line reference).\n\n"
            f"FILE SCOPE — Read ONLY the files listed in the manifest below. "
            f"Do not explore the whole repository (no unbounded `find`, `grep -r`, "
            f"or `ls -R` across the repo). If a criterion requires a file not listed "
            f"here, state that in the evidence instead of searching for it.\n\n"
            f"{file_scope}\n\n"
            f"{_SCANNING_BLOCK}"
            f"If a criterion has acceptable variance (implementation differs from original "
            f"spec but still satisfies user story intent), use verdict 'adjusted' instead of 'unmet'. "
            f"Include justification in the evidence field.\n\n"
            f"Criteria: {child_ac_list}"
        )
        try:
            result = _call_pi_and_maybe_log(
                child.get("id", ""), f"child:{child.get('id', '')}", prompt,
                model=resolved_model, pi_bin=pi_bin, debug_log=debug_log,
                enable_tools=True, timeout=timeout,
                ac_fallback_used=ac_fallback_used,
                child_screen=True,
            )
        except RuntimeError as exc:
            script_failure_callback("pi (child AC review)", exc)
            print(
                f"Warning: Pi call failed for child AC review: {exc}",
                file=sys.stderr,
            )
            result = {"verdict": "unmet", "evidence": "", "extracted_text": ""}
        # Use extracted_text (full response) instead of evidence (may be truncated)
        raw_text = result.get("extracted_text", "") or result.get("evidence", "") or result.get("text", "")
        batch = _extract_json_array(raw_text)
        if batch is None:
            try:
                batch = json.loads(raw_text)
            except json.JSONDecodeError:
                batch = []
        if isinstance(batch, list) and batch and any(
            isinstance(item, dict) and "index" in item for item in batch
        ):
            reviewed = {item["index"]: item for item in batch if isinstance(item, dict) and "index" in item}
            for i, ac in enumerate(child_acs):
                item = reviewed.get(i, {})
                child_ac_results.append({
                    "text": ac,
                    "verdict": _normalize_verdict(item.get("verdict", "unmet")),
                    "evidence": item.get("evidence", ""),
                })
        else:
            # Fallback: this path is reached when the Pi response was not a
            # parseable JSON array. Print a warning, log raw output, and use
            # 'partial' verdict with diagnostic evidence instead of silently
            # falling back to 'unmet' with empty evidence.
            # Infra-failure provenance: the batch JSON could not be parsed
            # (unparseable output, provider error, or concurrency-limit
            # timeout), so any 'No' derived from these verdicts must restore
            # the pre-audit state rather than demote (SA-0MSG9SLGI002OF7V).
            if ac_fallback_used is not None:
                ac_fallback_used.set()
            if result.get("_provider_error"):
                print(
                    "Warning: Pi provider error during child AC evaluation — "
                    "falling back to 'partial' verdict",
                    file=sys.stderr,
                )
            else:
                print(
                    "Warning: Unparseable Pi output for AC evaluation — "
                    "falling back to 'partial' verdict",
                    file=sys.stderr,
                )
            if debug_log:
                try:
                    target = Path(debug_log) if debug_log else _default_debug_log_path(child.get("id", "unknown"), f"child_{child.get('id', 'unknown')}_ac_fallback")
                    _write_debug_log(target, {
                        "issue_id": child.get("id"),
                        "child_id": child.get("id"),
                        "context": "child_ac_fallback",
                        "reason": "provider_error" if result.get("_provider_error") else "parse_failure",
                        "raw_text": raw_text,
                        "result_verdict": result.get("verdict"),
                        "result_evidence": result.get("evidence", "")[:500],
                        "provider_error": result.get("_provider_error_message"),
                    })
                except Exception:  # noqa: S110, BLE001 -- optional enhancement, ignore on failure
                    pass
            # When batched parsing fails, the root-level verdict from Pi
            # cannot be trusted to represent each AC individually. Override
            # verdict to 'partial' but preserve any diagnostic evidence
            # from the root-level result (e.g., a timeout message).
            if result.get("_provider_error"):
                provider_error = result.get("_provider_error_message", "unknown")
                evidence = (
                    f"Pi provider error: {provider_error} — "
                    "criterion could not be evaluated."
                )
            else:
                outer_evidence = result.get("evidence", "")
                if outer_evidence:
                    evidence = (
                        f"Pi model output could not be parsed — raw output logged. "
                        f"Root-level diagnostic: {outer_evidence[:500]}"
                    )
                else:
                    evidence = "Pi model output could not be parsed — raw output logged"
            for ac in child_acs:
                child_ac_results.append({
                    "text": ac,
                    "verdict": "partial",
                    "evidence": evidence,
                })
    return ci, child_ac_results


def _parent_has_gaps(ac_results: list[dict]) -> bool:
    """Return True when the parent audit has any gap (unmet/partial AC).

    ``adjusted`` verdicts are acceptable variance and do NOT count as gaps
    (consistent with ``_ACCEPTABLE_VERDICTS``). The parent-first child
    pass-through (SA-0MSKB6VJA005N43F) uses this to decide whether children
    inherit the parent's pass or are audited: parent passes with no gaps →
    all children inherit passed; parent has gaps → only gap-mapped children
    are audited.
    """
    return any(r.get("verdict") not in _ACCEPTABLE_VERDICTS for r in ac_results)


def _child_content_changed(runner: Runner, child_id: str,
                           worklog_dir: str | None = None,
                           work_item: dict | None = None) -> bool:
    """Return True when a child's own content changed since its last audit.

    Uses the Feature 1 content-based freshness gate (SA-0MSKB6US1009CNHT): a
    stored audit whose content fingerprint no longer matches means the
    child's content changed, so the child must NOT silently inherit the
    parent's pass (SA-0MSKB6VJA005N43F AC6). Children with no stored audit
    (never audited) return False — nothing to compare, so inheritance is
    safe. A child with a content-fresh audit returns False too.
    """
    fresh_report = _check_audit_freshness(
        runner, child_id, worklog_dir=worklog_dir, work_item=work_item,
    )
    if fresh_report is not None:
        return False  # content-fresh audit exists → unchanged
    # No content-fresh audit: distinguish "never audited" from "content
    # changed" by checking whether an audit record exists at all.
    try:
        data = _run_wl(runner, ["wl", "audit-show", child_id, "--json"],
                       worklog_dir=worklog_dir)
    except RuntimeError:
        return False  # cannot determine → treat as unchanged (fail-open)
    audit = data.get("audit") if isinstance(data, dict) else None
    return audit is not None  # audit exists but content-fresh gate failed → changed


def _map_gaps_to_children(ac_results: list[dict],
                          child_results: list[dict]) -> list[str]:
    """Map parent gap ACs to the child work items that own the affected files.

    For each gap AC (unmet/partial), extract file references from its
    evidence (``path/file.ext:line``) and from the parent's Key Files, then
    return the child ids whose own description Key Files intersect those
    references. When no mapping can be determined (no file refs, no child Key
    Files), ALL children are returned so nothing is silently skipped
    (conservative — a not-ready child still blocks the parent).
    """
    gap_refs: set[str] = set()
    for r in ac_results:
        if r.get("verdict") in _ACCEPTABLE_VERDICTS:
            continue
        evidence = r.get("evidence", "") or ""
        for m in re.finditer(r"([\w./-]+\.\w+)(?::\d+)?", evidence):
            gap_refs.add(m.group(1))
    if not gap_refs:
        # No evidence file refs to map from — conservative: audit all children.
        return [c.get("id", "") for c in child_results if c.get("id")]

    mapped: list[str] = []
    for child in child_results:
        child_key_files = _extract_key_files(child.get("description", "") or "")
        if any(kf in gap_refs for kf in child_key_files):
            mapped.append(child.get("id", ""))
    if not mapped:
        # No child Key Files intersect the gap refs — conservative: audit all.
        return [c.get("id", "") for c in child_results if c.get("id")]
    return mapped


# ---------------------------------------------------------------------------
# Small/low-risk Phase 2 skip helper (SA-0MSQ026T3009QY2L)
# ---------------------------------------------------------------------------

_SMALL_EFFORTS = frozenset({"Extra Small", "Small", "XS", "S"})
_LOW_RISK = frozenset({"Low", "low", "L"})


def _is_low_risk_small(effort: str | None, risk: str | None) -> bool:
    """Return True when effort is XS/Small AND risk is Low.

    This is the **only** exception to "Phase 2 mandatory": when a work item
    is both small (Extra Small or Small effort) **and** low risk, deep code
    analysis is skipped — Phase 1 verdicts stand unchanged with a skip
    diagnostic.

    Fail-closed: when either value is missing or unknown the function
    returns ``False`` so Phase 2 runs as usual.
    """
    if effort is None or risk is None:
        return False
    effort_stripped = effort.strip()
    risk_stripped = risk.strip()
    return (
        effort_stripped in _SMALL_EFFORTS and risk_stripped in _LOW_RISK
    )


def _annotate_skip_evidence(ac_results: list[dict], note: str) -> list[dict]:
    """Append a Phase 2 skip note to each AC's evidence (verdicts unchanged).

    Returns a new list; the verdict of every AC is preserved verbatim — the
    skip only records why deep analysis did not run (SA-0MSQ026T3009QY2L).
    """
    updated: list[dict] = []
    for ac in ac_results:
        item = dict(ac)
        evidence = item.get("evidence", "") or ""
        if evidence:
            evidence = f"{evidence}; {note}"
        else:
            evidence = note
        item["evidence"] = evidence
        updated.append(item)
    return updated


def _has_phase1_blocking_issues(cq_findings: list[dict], child_results: list[dict],
                                fp_screen_results: list[dict] | None = None) -> tuple[bool, str]:
    """Check whether Phase 1 automated screening has blocking issues.

    Returns (blocked, reason). If blocked, Phase 2 deep analysis should be
    skipped and all 'met' verdicts demoted to 'partial'.

    Blocking issues include:
    - Critical/high code quality findings (unless the false-positive screen
      classified them ``confident-false-positive``; ``uncertain`` findings
      remain blocking with the candidate-false-positive annotation,
      SA-0MST01O4G002VPBR AC4)
    - Children not in in_review/done stage
    - Active children in pre-review stages whose persisted audit says
      "Ready to close: No" (children in ``in_review`` stage are exempt
      from child audit verdict checking — per the audit spec, only
      pre-review stages block)
    """
    # Check code quality findings
    fp_results = fp_screen_results or []
    for f in cq_findings:
        if f.get("severity") in ("critical", "high"):
            classification = _fp_classification_for(f, fp_results)
            if classification == "confident-false-positive":
                continue
            if classification == "uncertain":
                return True, (
                    f"Critical/high code quality finding: {f.get('file', '?')}:{f.get('line', 0)} "
                    f"— {f.get('message', '')} — {FP_CANDIDATE_ANNOTATION}"
                )
            return True, f"Critical/high code quality finding: {f.get('file', '?')}:{f.get('line', 0)} — {f.get('message', '')}"

    # Check children stages — skip deleted children and inherited-pass
    # children (parent-first pass-through, SA-0MSKB6VJA005N43F: an inherited
    # child is reviewed by virtue of the parent's pass).
    active_children = [
        c for c in child_results
        if c.get("stage") not in ("", None) and c.get("status") != "deleted"
        and not c.get("inherited_pass")
    ]
    blocked_children = [
        c for c in active_children
        if c.get("stage") not in ("in_review", "done")
    ]
    if blocked_children:
        names = ", ".join(f"{c.get('title', '?')} ({c.get('stage', '?')})" for c in blocked_children[:3])
        return True, f"Children not in in_review/done stage: {names}"

    # Check each active child's persisted audit verdict
    # A child with child_audit_ready=False means its own audit says "not ready"
    # Children in in_review stage are exempt from this check (per audit spec,
    # in_review children do NOT block parent closure — only pre-review stages block).
    for c in active_children:
        # Skip in_review children — their audit verdicts do not block Phase 1
        if c.get("stage") == "in_review":
            continue
        car = c.get("child_audit_ready")
        if car is False:
            return True, (
                f"Child '{c.get('title', '?')}' ({c.get('id', '?')}) audit says "
                "'not ready to close' — block parent closure"
            )

    return False, ""

def _build_issue_json(issue: dict, ac_results: list[dict],
                      child_results: list[dict],
                      code_quality_findings: list[dict] | None = None,
                      code_quality_fixes_applied: int = 0,
                      fp_screen_results: list[dict] | None = None,
                      remediation_results: dict | None = None,
                      phase2_completed: bool = False,
                      phase2_skip_note: str | None = None) -> dict:
    """Build structured JSON payload for issue-mode audit.

    Ready-to-close logic:
      - All acceptance criteria (parent + children) must be ``met`` or ``adjusted``.
        ``adjusted`` criteria represent acceptable variance and do not block closure.
      - Critical/high code quality findings block closure, unless the
        false-positive screen classified them ``confident-false-positive``
        (uncertain findings stay blocking; SA-0MST01O4G002VPBR AC4).
      - Children in ``in_review`` or ``done`` stage are exempt from child
        audit verdict checks. Per the audit spec, children in ``in_review``
        do NOT block closure — only pre-review stages (``idea``,
        ``intake_complete``, ``plan_complete``) block.
    """
    all_ac_acceptable = all(
        r["verdict"] in _ACCEPTABLE_VERDICTS
        for r in ac_results + [c for cr in child_results for c in cr.get("ac_results", [])]
    )
    # Check that all active children are in in_review or done stage.
    # Children that inherited the parent's pass (parent-first pass-through,
    # SA-0MSKB6VJA005N43F) count as reviewed by virtue of the parent.
    active_children = [c for c in child_results if c.get("stage") not in ("", None)]
    all_children_reviewed = all(
        c.get("stage") in ("in_review", "done") or c.get("inherited_pass")
        for c in active_children
    )

    # Check each non-exempt child's persisted audit verdict
    # Exempt children: status=deleted (wl delete), completed/done (already closed),
    # and those in in_review stage (per spec, in_review children do not
    # block parent closure — only pre-review stages block).
    def _is_exempt(c: dict) -> bool:
        # Deleted children are fully closed
        if c.get("status") == "deleted":
            return True
        # Completed/done children are fully closed
        if c.get("status") == "completed" and c.get("stage") == "done":
            return True
        # Children in in_review stage should not have their audit verdicts
        # block parent closure (per audit spec)
        return c.get("stage") == "in_review"
    non_exempt_children = [c for c in active_children if not _is_exempt(c)]
    any_child_audit_not_ready = any(
        c.get("child_audit_ready") is False
        for c in non_exempt_children
    )

    # Code quality blocking
    cq_findings = code_quality_findings or []
    has_blocking_cq = bool(_effective_blocking_findings(
        cq_findings, fp_screen_results or []
    ))

    ready = all_ac_acceptable and all_children_reviewed and not has_blocking_cq and not any_child_audit_not_ready

    all_criteria = ac_results + [c for cr in child_results for c in cr.get("ac_results", [])]
    unmet_count = sum(1 for r in all_criteria if r["verdict"] == VERDICT_UNMET)
    adjusted_count = sum(1 for r in all_criteria if r["verdict"] == VERDICT_ADJUSTED)

    phase2_note = (
        f" Phase 2 deep analysis skipped: {phase2_skip_note}."
        if phase2_skip_note
        else (" Deep analysis completed." if phase2_completed else " Phase 2 skipped.")
    )
    if all_ac_acceptable:
        if adjusted_count > 0:
            summary = (
                f"All {len(ac_results)} acceptance criteria acceptable "
                f"({adjusted_count} with acceptable variance).{phase2_note}"
            )
        else:
            summary = f"All {len(ac_results)} acceptance criteria met.{phase2_note}"
    else:
        summary = (
            f"{unmet_count} of {len(ac_results)} acceptance criteria not met.{phase2_note}"
        )

    return {
        "ready_to_close": ready,
        "summary": summary,
        "acceptance_criteria": ac_results,
        "children": child_results,
        "code_quality": {
            "total_findings": len(cq_findings),
            "fixes_applied": code_quality_fixes_applied,
            "findings": cq_findings,
            "false_positive_screen": [
                {
                    "index": e.get("index", 0),
                    "file": e.get("finding", {}).get("file", "?"),
                    "line": e.get("finding", {}).get("line", 0),
                    "code": e.get("finding", {}).get("code", "?"),
                    "linter": e.get("finding", {}).get("linter", "?"),
                    "severity": e.get("finding", {}).get("severity", "?"),
                    "classification": e.get("classification", "uncertain"),
                    "justification": e.get("justification", ""),
                    "remediable": e.get("remediable", False),
                    "screen_failed": e.get("screen_failed", False),
                }
                for e in (fp_screen_results or [])
            ],
            "remediation": (remediation_results or {}),
        },
        "pipeline": {
            "phase1_completed": True,
            "phase2_completed": phase2_completed,
        },
    }


def _deep_analyze_child(
    ci: int,
    child: dict,
    resolved_model: str,
    pi_bin: str,
    debug_log: str | None,
    timeout: int | None,
    runner: Runner,
    ac_fallback_used: threading.Event | None = None,
    green_run_block: str | None = None,
    max_citations_per_ac: int = _DEFAULT_MAX_CITATIONS_PER_AC,
) -> tuple[int, dict, bool]:
    """Run Phase 2 deep analysis for a single child (worker for parallelism).

    Returns ``(ci, updated_child, timeout_occurred)``. Never raises on Pi
    failures: a ``RuntimeError`` from ``_call_pi_and_maybe_log`` falls back to
    the child's existing ``ac_results`` unchanged. A timeout marks the child's
    ACs ``partial`` and reports ``timeout_occurred=True`` (so the caller can
    set ``phase2_completed=False``).

    *green_run_block* is the GREEN-RUN attestation block (or ``None``); when
    set it is injected into the child deep-analysis prompt so the model may
    mark execution-dependent criteria met based on the operator attestation.

    *max_citations_per_ac* bounds the file:line evidence citations the model
    may emit per criterion (prompt-level only, LP-0MSQ32WM5000NCB7).
    """
    child_acs = child.get("ac_results", [])
    if not child_acs:
        return ci, child, False

    child_ac_list = json.dumps([
        {"index": i, "text": r["text"], "initial_verdict": r["verdict"]}
        for i, r in enumerate(child_acs)
    ])
    child_file_scope = _build_file_scope_manifest(child, child_acs, runner=runner)
    child_prompt = (
        "[READ-ONLY AUDIT] [PHASE 2 — DEEP CODE ANALYSIS — CHILD] "
        "Do NOT close, modify, create, or delete any work items. "
        "Return ONLY a structured JSON array.\n\n"
        f"Deep code analysis for child: {child.get('title', '')} ({child.get('id', '')})\n\n"
        "FILE SCOPE — Read ONLY the files listed in the manifest below. "
        "Do not explore the whole repository. If a criterion requires a "
        "file not listed here, state that in the evidence instead of "
        "searching for it.\n\n"
        f"{child_file_scope}\n\n"
        f"{_SCANNING_BLOCK}"
        f"{green_run_block or ''}"
        f"{_max_citations_prompt_snippet(max_citations_per_ac)}"
        "For each criterion, read the actual implementation files and verify "
        "the code genuinely satisfies the stated requirements. "
        "Use the same verdict guidance as the parent deep analysis.\n\n"
        f"Criteria: {child_ac_list}"
    )

    try:
        child_result = _call_pi_and_maybe_log(
            child.get("id", ""), f"phase2_child:{ci}", child_prompt,
            model=resolved_model, pi_bin=pi_bin, debug_log=debug_log,
            enable_tools=True, timeout=timeout,
            max_retries=_PHASE2_MAX_RETRIES,
            ac_fallback_used=ac_fallback_used,
            ac_count=len(child_acs),
        )
    except RuntimeError:
        return ci, child, False

    # Handle child timeout: mark all child ACs as partial
    if child_result.get("_timeout"):
        if ac_fallback_used is not None:
            ac_fallback_used.set()
        print(
            f"Warning: Child deep analysis timed out for {child.get('id', '')}",
            file=sys.stderr,
        )
        timeout_acs = []
        for ac in child_acs:
            timeout_acs.append({
                "text": ac.get("text", ""),
                "verdict": VERDICT_PARTIAL,
                "evidence": "Deep analysis timed out \u2014 manual review required.",
            })
        updated = dict(child)
        updated["ac_results"] = timeout_acs
        return ci, updated, True

    # Handle provider errors (e.g. finish_reason: error) before parsing.
    # Mirror the parent phase2_deep path: the model never emitted its
    # structured output, so degrade all child ACs to partial instead of
    # silently keeping Phase 1 verdicts (met-only-when-confirmed invariant).
    if child_result.get("_provider_error"):
        if ac_fallback_used is not None:
            ac_fallback_used.set()
        provider_error = child_result.get("_provider_error_message", "unknown")
        print(
            f"Warning: Child deep analysis provider error for {child.get('id', '')}: "
            f"{provider_error}",
            file=sys.stderr,
        )
        error_acs = []
        for ac in child_acs:
            error_acs.append({
                "text": ac.get("text", ""),
                "verdict": VERDICT_PARTIAL,
                "evidence": f"Pi provider error: {provider_error} \u2014 manual review required.",
            })
        updated = dict(child)
        updated["ac_results"] = error_acs
        # Same incomplete signal as timeout so phase2_completed=False propagates.
        return ci, updated, True

    child_raw = (
        child_result.get("extracted_text", "")
        or child_result.get("evidence", "")
        or child_result.get("text", "")
    )
    child_batch = _extract_json_array(child_raw)
    if child_batch is None:
        try:
            child_batch = json.loads(child_raw)
        except json.JSONDecodeError:
            child_batch = []

    updated_child_acs = list(child_acs)
    if isinstance(child_batch, list):
        reviewed = {
            item["index"]: item
            for item in child_batch
            if isinstance(item, dict) and "index" in item
        }
        for i in range(len(updated_child_acs)):
            item = reviewed.get(i, {})
            deep_verdict = _normalize_verdict(item.get("verdict", ""))
            deep_evidence = item.get("evidence", "")
            if deep_verdict:
                initial = updated_child_acs[i]["verdict"]
                if initial == VERDICT_MET and deep_verdict == VERDICT_MET:
                    updated_child_acs[i] = {
                        "text": updated_child_acs[i]["text"],
                        "verdict": VERDICT_MET,
                        "evidence": deep_evidence or updated_child_acs[i].get("evidence", ""),
                    }
                elif initial == VERDICT_MET and deep_verdict != VERDICT_MET:
                    updated_child_acs[i] = {
                        "text": updated_child_acs[i]["text"],
                        "verdict": deep_verdict,
                        "evidence": f"Phase 1: {updated_child_acs[i].get('evidence', '')}; Phase 2 deep analysis: {deep_evidence}",
                    }
                else:
                    updated_child_acs[i] = {
                        "text": updated_child_acs[i]["text"],
                        "verdict": deep_verdict,
                        "evidence": deep_evidence or updated_child_acs[i].get("evidence", ""),
                    }

    updated = dict(child)
    updated["ac_results"] = updated_child_acs
    return ci, updated, False


def _apply_deep_verdicts(
    initial_acs: list[dict], reviewed: dict[int, dict],
) -> list[dict]:
    """Apply Phase 2 deep-analysis verdicts to a list of ACs.

    Standard merge semantics shared by the parent and child Phase 2 paths:
      - Phase 1 met + Phase 2 met      -> met (deep evidence wins)
      - Phase 1 met + Phase 2 not met  -> downgrade to the deep verdict
      - otherwise                       -> deep verdict (deep override)
    Entries absent from *reviewed* keep their initial verdict unchanged.
    """
    updated = list(initial_acs)
    for i in range(len(updated)):
        item = reviewed.get(i, {})
        deep_verdict = _normalize_verdict(item.get("verdict", ""))
        deep_evidence = item.get("evidence", "")
        if not deep_verdict:
            continue
        initial = updated[i]["verdict"]
        if initial == VERDICT_MET and deep_verdict == VERDICT_MET:
            updated[i] = {
                "text": updated[i]["text"],
                "verdict": VERDICT_MET,
                "evidence": deep_evidence or updated[i].get("evidence", ""),
            }
        elif initial == VERDICT_MET and deep_verdict != VERDICT_MET:
            updated[i] = {
                "text": updated[i]["text"],
                "verdict": deep_verdict,
                "evidence": (
                    f"Phase 1: {updated[i].get('evidence', '')}; "
                    f"Phase 2 deep analysis: {deep_evidence}"
                ),
            }
        else:
            updated[i] = {
                "text": updated[i]["text"],
                "verdict": deep_verdict,
                "evidence": deep_evidence or updated[i].get("evidence", ""),
            }
    return updated


def _run_batch_phase2(
    issue: dict,
    ac_results: list[dict],
    pending: list[tuple[int, dict]],
    updated_children: list[dict],
    resolved_model: str,
    pi_bin: str,
    debug_log: str | None,
    timeout: int | None,
    runner: Runner,
    ac_fallback_used: threading.Event | None = None,
    green_run_block: str | None = None,
    max_citations_per_ac: int = _DEFAULT_MAX_CITATIONS_PER_AC,
) -> tuple[list[dict], list[dict], bool] | None:
    """Attempt Phase 2 batch deep analysis (P6).

    Folds the parent ACs and each pending child's ACs into ONE indexed list
    and makes a single ``phase2_batch`` pi call, then routes the indexed
    verdicts back to the parent and per-child AC lists.

    Returns ``(updated_ac, updated_children, True)`` on success, or ``None``
    when the batch call fails (RuntimeError, timeout, provider error, or
    unparseable output) so the caller falls back to the existing per-child
    deep-analysis path.

    *green_run_block* is the GREEN-RUN attestation block (or ``None``); when
    set it is injected into the batch prompt.

    *max_citations_per_ac* bounds the file:line evidence citations the model
    may emit per criterion (prompt-level only, LP-0MSQ32WM5000NCB7).
    """
    ac_list: list[dict] = []
    for i, r in enumerate(ac_results):
        ac_list.append({
            "index": i,
            "scope": "parent",
            "local_index": i,
            "text": r["text"],
            "initial_verdict": r["verdict"],
        })
    next_index = len(ac_results)
    child_ranges: list[tuple[int, int, int]] = []
    child_manifest_blocks: list[tuple[int, dict, str]] = []
    for ci, child in pending:
        child_acs = child.get("ac_results", [])
        start = next_index
        for j, r in enumerate(child_acs):
            ac_list.append({
                "index": start + j,
                "scope": f"child:{ci}",
                "local_index": j,
                "text": r["text"],
                "initial_verdict": r["verdict"],
            })
        next_index += len(child_acs)
        child_ranges.append((ci, start, len(child_acs)))
        child_manifest_blocks.append((ci, child, _build_file_scope_manifest(
            child, child_acs, runner=runner,
        )))

    parent_manifest = _build_file_scope_manifest(issue, ac_results, runner=runner)

    batch_prompt = (
        "[READ-ONLY AUDIT] [PHASE 2 — DEEP CODE ANALYSIS — BATCH] "
        "Do NOT close, modify, create, or delete any work items. "
        "Return ONLY a structured JSON array.\n\n"
        "Deep code analysis for the parent work item and its active children. "
        "The criteria below form ONE indexed list: the parent's acceptance "
        "criteria first, then each child's criteria in order. Return one "
        "object per criterion with keys 'index' (the index given here), "
        "'verdict' (met/unmet/partial/adjusted) and 'evidence' (a file:line "
        "reference).\n\n"
        "PARENT FILE SCOPE — Read ONLY the files listed below; do not explore "
        "the whole repository.\n\n"
        f"{parent_manifest}\n\n"
    )
    for _ci, child, manifest in child_manifest_blocks:
        batch_prompt += (
            f"CHILD FILE SCOPE — {child.get('title', '')} "
            f"({child.get('id', '')}):\n{manifest}\n\n"
        )
    batch_prompt += (
        f"{_SCANNING_BLOCK}"
        f"{green_run_block or ''}"
        f"{_max_citations_prompt_snippet(max_citations_per_ac)}"
        "For each criterion, read the actual implementation files and verify "
        "the code genuinely satisfies the stated requirement. Provide a "
        "specific file:line reference as evidence.\n\n"
        "Verdict guidance: 'met' only if the code genuinely satisfies the "
        "criterion; 'unmet' if not satisfied at all; 'partial' if partially "
        "satisfied; 'adjusted' if the implementation differs from the original "
        "spec but the user story intent is preserved.\n\n"
        f"Criteria: {json.dumps(ac_list)}"
    )

    try:
        result = _call_pi_and_maybe_log(
            issue.get("id", ""), "phase2_batch", batch_prompt,
            model=resolved_model, pi_bin=pi_bin, debug_log=debug_log,
            enable_tools=True, timeout=timeout,
            max_retries=_PHASE2_MAX_RETRIES,
            ac_fallback_used=ac_fallback_used,
            ac_count=len(ac_list),
        )
    except RuntimeError:
        return None

    if result.get("_timeout") or result.get("_provider_error"):
        if ac_fallback_used is not None:
            ac_fallback_used.set()
        return None

    raw = (
        result.get("extracted_text", "")
        or result.get("evidence", "")
        or result.get("text", "")
    )
    batch = _extract_json_array(raw)
    if batch is None:
        try:
            batch = json.loads(raw)
        except json.JSONDecodeError:
            # Unparseable output: fall back to the per-child path rather
            # than silently succeeding with an empty verdict map.
            if ac_fallback_used is not None:
                ac_fallback_used.set()
            return None
    if not isinstance(batch, list) or not batch:
        if ac_fallback_used is not None:
            ac_fallback_used.set()
        return None
    if not any(isinstance(item, dict) and "index" in item for item in batch):
        if ac_fallback_used is not None:
            ac_fallback_used.set()
        return None

    reviewed = {
        item["index"]: item
        for item in batch
        if isinstance(item, dict) and "index" in item
    }

    parent_reviewed = {i: reviewed.get(i, {}) for i in range(len(ac_results))}
    updated_ac = _apply_deep_verdicts(ac_results, parent_reviewed)

    for ci, start, count in child_ranges:
        child_reviewed = {j: reviewed.get(start + j, {}) for j in range(count)}
        child_acs = updated_children[ci].get("ac_results", [])
        new_child = dict(updated_children[ci])
        new_child["ac_results"] = _apply_deep_verdicts(child_acs, child_reviewed)
        updated_children[ci] = new_child

    return updated_ac, updated_children, True


def _run_phase2_deep_analysis(
    issue: dict,
    ac_results: list[dict],
    child_results: list[dict],
    resolved_model: str,
    pi_bin: str = "pi",
    debug_log: str | None = None,
    script_failure_callback=None,
    timeout: int | None = None,
    runner: Runner | None = None,
    batch_phase2: bool = False,
    worklog_dir: str | None = None,
    ac_fallback_used: threading.Event | None = None,
    green_run_block: str | None = None,
    skip_parent_deep: bool = False,
    owning_root: Path | None = None,
    max_citations_per_ac: int | None = None,
) -> tuple[list[dict], list[dict], bool]:
    """Run Phase 2 deep code analysis.

    Calls Pi with a detailed prompt asking the model to read the actual
    implementation files and verify each acceptance criterion against
    what the code actually does. The prompt includes a file-scope manifest
    (Key Files, git changed files, repo index, Phase 1 evidence refs) so
    the model verifies in-scope files instead of exploring the whole repo.

    *runner* is used for git queries when building the file-scope manifest
    and defaults to ``_default_runner``.

    *green_run_block* is the GREEN-RUN attestation block (or ``None``); when
    set it is injected into the parent deep prompt and forwarded to the
    child (``phase2_child``) and batch (``phase2_batch``) prompts so the
    model may mark execution-dependent criteria met based on the operator
    attestation.

    *skip_parent_deep* (SA-0MSKB6VJA005N43F) skips the parent deep call and
    only runs child deep analysis. The parent-first flow runs parent Phase 2
    FIRST (parent-only), then passes the already-deep-verified parent
    ``ac_results`` back in with the gap-mapped children so the parent call is
    not duplicated.

    Small/low-risk children (SA-0MSQ026T3009QY2L): children with ``effort``
    ∈ {Extra Small, Small} AND ``risk`` = Low are dropped from the Phase 2
    pending list — their Phase 1 ``ac_results`` stand unchanged with the
    skip reason annotated into the AC evidence. Non-qualifying children
    (and children with missing/unknown effort or risk — fail-closed) still
    get deep analysis. The parent's own skip is decided by the caller's
    Phase 2 gate, which passes ``skip_parent_deep=True`` here when the
    parent qualifies.

    *owning_root* is the project root that owns the audited item (resolved
    by the launch-context guard); when omitted it is re-resolved from the
    issue id. The FILE SCOPE manifest is validated against it and an
    ``AuditScopeError`` is raised when the manifest lacks the item repo
    (LP-0MSQ32HNR007AI6B) — the caller aborts instead of emitting 'unmet'
    verdicts from a wrong scope.

    *max_citations_per_ac* bounds the file:line evidence citations the model
    may emit per criterion in the parent/child/batch deep prompts (default:
    resolved via ``_resolve_max_citations_per_ac``). Prompt-level only —
    verdict semantics and the canonical report format are unchanged
    (LP-0MSQ32WM5000NCB7).

    Returns (updated_ac_results, updated_child_results, phase2_completed).
    The ``phase2_completed`` flag is ``False`` when the Pi call times out,
    allowing the caller to set appropriate diagnostic evidence.
    """
    if runner is None:
        runner = _default_runner

    if max_citations_per_ac is None:
        max_citations_per_ac = _resolve_max_citations_per_ac()

    # Active children needing Phase 2 deep analysis (shared by the batch
    # path and the per-child path below).
    # - Completed/done children are skipped (already closed).
    # - child_audit_ready=True children are skipped (P2 reuse — their own
    #   fresh audit already verified the code).
    # - child_audit_not_ready=True children are skipped (P12 reuse — their
    #   own fresh audit already deep-analyzed the ACs and returned an
    #   explicit 'not ready to close' verdict; the duplicated phase2_child
    #   call is skipped and the child's own persisted findings reused).
    # - Children without ac_results are skipped (nothing to verify).
    updated_children = list(child_results)
    pending: list[tuple[int, dict]] = []
    for ci, child in enumerate(updated_children):
        if child.get("status") == "completed" and child.get("stage") == "done":
            continue
        if child.get("child_audit_ready") is True:
            continue
        if child.get("child_audit_not_ready") is True:
            # P12: this child's own fresh audit produced an explicit
            # 'not ready to close' verdict — its own pipeline already ran
            # deep analysis on these ACs. Skip the duplicated phase2_child
            # call and reuse the child's own persisted audit findings,
            # falling back to the Phase 1 screening results if the child's
            # audit report table cannot be parsed.
            child["ac_results"] = _child_acs_from_own_audit(
                child, runner, worklog_dir=worklog_dir,
                fallback=child.get("ac_results", []),
            )
            continue
        if not child.get("ac_results"):
            continue
        if _is_low_risk_small(child.get("effort"), child.get("risk")):
            # Small effort + Low risk — skip this child's Phase 2 deep
            # analysis (SA-0MSQ026T3009QY2L). Phase 1 verdicts stand
            # unchanged; annotate the AC evidence with the skip reason.
            print(
                f"Skipping Phase 2 deep analysis for child "
                f"{child.get('id', '')}: effort={child.get('effort')}, "
                f"risk={child.get('risk')}. Phase 1 verdicts stand unchanged.",
                file=sys.stderr,
            )
            child["ac_results"] = _annotate_skip_evidence(
                child.get("ac_results", []),
                f"Phase 2 deep analysis skipped (effort={child.get('effort')}, "
                f"risk={child.get('risk')}): small, low-risk item per "
                f"SA-0MSQ026T3009QY2L. Phase 1 verdict stands.",
            )
            continue
        pending.append((ci, child))

    # Batch mode (P6): fold parent + pending child ACs into one indexed
    # call. On any failure fall back to the per-child path below.
    # When *skip_parent_deep* is set the parent was already deep-verified
    # in a prior parent-only call (SA-0MSKB6VJA005N43F); the batch path
    # re-analyzes the parent ACs, so skip it and use the per-child path.
    if batch_phase2 and pending and not skip_parent_deep:
        batch_outcome = _run_batch_phase2(
            issue, ac_results, pending, updated_children,
            resolved_model, pi_bin, debug_log, timeout, runner,
            ac_fallback_used=ac_fallback_used,
            green_run_block=green_run_block,
            max_citations_per_ac=max_citations_per_ac,
        )
        if batch_outcome is not None:
            return batch_outcome

    if not skip_parent_deep:
        file_scope = _build_file_scope_manifest(issue, ac_results, runner=runner)
        # Validate the Phase 2 FILE SCOPE manifest covers the item repository
        # (LP-0MSQ32HNR007AI6B): a wrong scope must abort with a scope error
        # instead of emitting misleading 'unmet' verdicts.
        if owning_root is None:
            owning_root = _resolve_owning_project_root(issue.get("id", ""))
        scope_error = _validate_file_scope_manifest(file_scope, owning_root)
        if scope_error:
            raise AuditScopeError(scope_error)

        # Build a detailed prompt for deep analysis
        ac_list_json = json.dumps([
            {"index": i, "text": r["text"], "initial_verdict": r["verdict"]}
            for i, r in enumerate(ac_results)
        ])

        prompt = (
            "[READ-ONLY AUDIT] [PHASE 2 — DEEP CODE ANALYSIS] "
            "You are performing a deep code analysis. "
            "Do NOT close, modify, create, or delete any work items. "
            "Do NOT execute any wl, git, or other state-modifying commands. "
            "Return ONLY a structured JSON array.\n\n"
            "Phase 1 automated screening has PASSED. You must now perform deep code analysis.\n\n"
            "FILE SCOPE — Read ONLY the files listed in the manifest below. "
            "Do not explore the whole repository (no unbounded `find`, `grep -r`, "
            "or `ls -R` across the repo). If a criterion requires a file not listed "
            "here, state that in the evidence instead of searching for it.\n\n"
            f"{file_scope}\n\n"
            f"{_SCANNING_BLOCK}"
            f"{green_run_block or ''}"
            f"{_max_citations_prompt_snippet(max_citations_per_ac)}"
            "For each acceptance criterion:\n"
            "1. **Read the actual implementation files** mentioned in or implied by the criterion.\n"
            "2. **Verify the code actually does what the criterion claims.**\n"
            "3. **Check for gaps between documented behavior and actual behavior.**\n"
            "4. **Provide a specific file:line reference** as evidence.\n\n"
            "Instructions:\n"
            "- Use 'met' ONLY if the code genuinely satisfies the criterion.\n"
            "- Use 'unmet' if the criterion is not satisfied at all.\n"
            "- Use 'partial' if the criterion is partially satisfied (e.g., documented but not implemented, or implemented with gaps).\n"
            "- Use 'adjusted' if the implementation differs from the original specification but the user story intent is preserved.\n"
            "- Evidence MUST include a file path and line number. If no line number is available, state why.\n"
            "- If a criterion says 'X is implemented' but the code only has scaffolding/stubs, use 'partial' not 'met'.\n\n"
            f"Criteria: {ac_list_json}"
        )

        try:
            issue_id = issue.get("id", "")
            result = _call_pi_and_maybe_log(
                issue_id, "phase2_deep", prompt,
                model=resolved_model, pi_bin=pi_bin, debug_log=debug_log,
                enable_tools=True, timeout=timeout,
                max_retries=_PHASE2_MAX_RETRIES,
                ac_fallback_used=ac_fallback_used,
                ac_count=len(ac_results),
            )
        except RuntimeError as exc:
            # Phase 2 failure is non-fatal; log and fall back to Phase 1 results
            print(f"Warning: Phase 2 deep analysis failed: {exc}", file=sys.stderr)
            if script_failure_callback:
                script_failure_callback("pi (Phase 2 deep analysis)", exc)
            return ac_results, child_results, False

        # Check for timeout before attempting to parse results
        if result.get("_timeout"):
            if ac_fallback_used is not None:
                ac_fallback_used.set()
            evidence = result.get("evidence", "Deep analysis timed out.")
            print(f"Warning: Phase 2 deep analysis timed out: {evidence}", file=sys.stderr)
            if script_failure_callback:
                script_failure_callback(
                    "pi (Phase 2 deep analysis)",
                    RuntimeError(f"Phase 2 deep analysis timed out after {CALL_PI_TIMEOUT}s"),
                )
            # Mark all ACs as partial with timeout evidence
            timeout_acs = []
            for ac in ac_results:
                timeout_acs.append({
                    "text": ac.get("text", ""),
                    "verdict": VERDICT_PARTIAL,
                    "evidence": "Deep analysis timed out \u2014 manual review required.",
                })
            # Also mark all child ACs as partial
            timeout_children = []
            for child in child_results:
                child_acs = child.get("ac_results", [])
                updated_child_acs = []
                for ac in child_acs:
                    updated_child_acs.append({
                        "text": ac.get("text", ""),
                        "verdict": VERDICT_PARTIAL,
                        "evidence": "Deep analysis timed out \u2014 manual review required.",
                    })
                timeout_children.append(dict(child))
                timeout_children[-1]["ac_results"] = updated_child_acs
            return timeout_acs, timeout_children, False

        # Check for provider errors (e.g. finish_reason: error) before parsing.
        # The model never emitted its structured output, so treat all ACs as
        # partial with a provider-error diagnostic (distinct from a parse failure).
        if result.get("_provider_error"):
            if ac_fallback_used is not None:
                ac_fallback_used.set()
            provider_error = result.get("_provider_error_message", "unknown")
            print(
                f"Warning: Phase 2 deep analysis provider error: {provider_error}",
                file=sys.stderr,
            )
            if script_failure_callback:
                script_failure_callback(
                    "pi (Phase 2 deep analysis)",
                    RuntimeError(f"Pi provider error: {provider_error}"),
                )
            error_acs = []
            for ac in ac_results:
                error_acs.append({
                    "text": ac.get("text", ""),
                    "verdict": VERDICT_PARTIAL,
                    "evidence": f"Pi provider error: {provider_error} \u2014 manual review required.",
                })
            error_children = []
            for child in child_results:
                child_acs = child.get("ac_results", [])
                updated_child_acs = []
                for ac in child_acs:
                    updated_child_acs.append({
                        "text": ac.get("text", ""),
                        "verdict": VERDICT_PARTIAL,
                        "evidence": f"Pi provider error: {provider_error} \u2014 manual review required.",
                    })
                error_children.append(dict(child))
                error_children[-1]["ac_results"] = updated_child_acs
            return error_acs, error_children, False

        # Parse the batched result
        raw_text = (
            result.get("extracted_text", "")
            or result.get("evidence", "")
            or result.get("text", "")
        )
        batch = _extract_json_array(raw_text)
        if batch is None:
            try:
                batch = json.loads(raw_text)
            except json.JSONDecodeError:
                batch = []

        updated_ac = list(ac_results)
        if isinstance(batch, list):
            reviewed = {
                item["index"]: item
                for item in batch
                if isinstance(item, dict) and "index" in item
            }
            for i in range(len(updated_ac)):
                item = reviewed.get(i, {})
                deep_verdict = _normalize_verdict(item.get("verdict", ""))
                deep_evidence = item.get("evidence", "")
                if deep_verdict:
                    # Final verdict = Phase 1 passes AND Phase 2 confirms
                    initial = updated_ac[i]["verdict"]
                    if initial == VERDICT_MET and deep_verdict == VERDICT_MET:
                        updated_ac[i] = {
                            "text": updated_ac[i]["text"],
                            "verdict": VERDICT_MET,
                            "evidence": deep_evidence or updated_ac[i].get("evidence", ""),
                        }
                    elif initial == VERDICT_MET and deep_verdict != VERDICT_MET:
                        # Phase 1 said met, Phase 2 disagrees → downgrade
                        updated_ac[i] = {
                            "text": updated_ac[i]["text"],
                            "verdict": deep_verdict,
                            "evidence": f"Phase 1: {updated_ac[i].get('evidence', '')}; Phase 2 deep analysis: {deep_evidence}",
                        }
                    else:
                        # Use Phase 2 verdict (deep override for initial non-met)
                        updated_ac[i] = {
                            "text": updated_ac[i]["text"],
                            "verdict": deep_verdict,
                            "evidence": deep_evidence or updated_ac[i].get("evidence", ""),
                        }

    else:
        # Parent Phase 2 already completed in a prior parent-only call
        # (SA-0MSKB6VJA005N43F). Only the child deep calls below run.
        updated_ac = list(ac_results)
    # Also run deep analysis on active children
    child_timeout_occurred = False

    parallelism = _resolve_child_concurrency()

    def _merge_result(result: tuple[int, dict, bool]) -> None:
        nonlocal child_timeout_occurred
        ci, updated, timed_out = result
        updated_children[ci] = updated
        if timed_out:
            child_timeout_occurred = True

    if pending and parallelism > 1 and len(pending) > 1:
        # Bounded-concurrency parallel execution of independent child calls.
        # The parent deep-analysis call above already ran first; children are
        # independent of each other so they may run concurrently up to the cap.
        # A failure in one child must not prevent the others from completing,
        # so each worker is exception-safe (_deep_analyze_child never raises).
        with ThreadPoolExecutor(max_workers=parallelism) as executor:
            futures = [
                executor.submit(
                    _deep_analyze_child, ci, child, resolved_model, pi_bin,
                    debug_log, timeout, runner,
                    ac_fallback_used=ac_fallback_used,
                    green_run_block=green_run_block,
                    max_citations_per_ac=max_citations_per_ac,
                )
                for ci, child in pending
            ]
            for future in futures:
                try:
                    _merge_result(future.result())
                except Exception as exc:  # noqa: BLE001 -- isolation: one bad child must not fail the audit
                    print(
                        f"Warning: Child deep analysis worker failed: {exc}",
                        file=sys.stderr,
                    )
    else:
        # Sequential fallback: parallelism=1, a single pending child, or an
        # executor failure path — preserves the historical call order.
        for ci, child in pending:
            _merge_result(_deep_analyze_child(
                ci, child, resolved_model, pi_bin, debug_log, timeout, runner,
                ac_fallback_used=ac_fallback_used,
                green_run_block=green_run_block,
                max_citations_per_ac=max_citations_per_ac,
            ))

    return updated_ac, updated_children, not child_timeout_occurred


def _reask_verdict_array_once(
    issue: dict,
    ac_results: list[dict],
    resolved_model: str,
    pi_bin: str = "pi",
    debug_log: str | None = None,
    timeout: int | None = None,
) -> list[dict] | None:
    """Bounded re-ask (≤1 additional model call) to re-emit the verdict array.

    Triggered only when the final persistence step rejected the assembled
    report's verdict content (malformed JSON, SA-0MSF3RXUB000NLOI). Re-asks
    the model exactly ONCE to re-emit the acceptance-criteria verdicts as a
    single valid JSON array. Never re-runs the full audit pipeline.

    Returns the repaired ``ac_results`` (verdict/evidence refreshed per
    index, ``text`` preserved) on success, or None when the single re-ask
    fails (RuntimeError, timeout, provider error, or unparseable output) —
    the caller then keeps the fallback-persisted report.
    """
    if not ac_results:
        return None
    current = json.dumps([
        {
            "index": i,
            "text": r.get("text", ""),
            "verdict": r.get("verdict", "unmet"),
            "evidence": (r.get("evidence", "") or "")[:200],
        }
        for i, r in enumerate(ac_results)
    ])
    prompt = (
        "[READ-ONLY AUDIT] [VERDICT RE-EMIT] Do NOT close, modify, create, "
        "or delete any work items. Do NOT execute any wl, git, or other "
        "state-modifying commands. Return ONLY a single valid JSON array.\n\n"
        "The acceptance-criteria verdict array from a previous audit pass was "
        "rejected as malformed JSON during persistence. Re-emit the verdicts "
        "for the criteria below as ONE valid JSON array. Each element MUST "
        "have keys 'index' (integer), 'verdict' (one of met, unmet, partial, "
        "adjusted) and 'evidence' (a one-line note with file:line reference). "
        "Preserve the original verdicts unless the evidence strongly "
        "indicates a correction.\n\n"
        f"Criteria: {current}"
    )
    try:
        result = _call_pi_and_maybe_log(
            issue.get("id", ""), "verdict_reask", prompt,
            model=resolved_model, pi_bin=pi_bin, debug_log=debug_log,
            timeout=timeout,
        )
    except RuntimeError:
        return None

    if result.get("_timeout") or result.get("_provider_error"):
        return None

    raw = (
        result.get("extracted_text", "")
        or result.get("evidence", "")
        or result.get("text", "")
    )
    batch = _extract_json_array(raw)
    if batch is None:
        try:
            batch = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(batch, list) or not batch:
        return None
    if not any(isinstance(item, dict) and "index" in item for item in batch):
        return None

    reviewed = {
        item["index"]: item
        for item in batch
        if isinstance(item, dict) and "index" in item
    }
    repaired: list[dict] = []
    for i, r in enumerate(ac_results):
        item = reviewed.get(i, {})
        repaired.append({
            "text": r.get("text", ""),
            "verdict": _normalize_verdict(
                item.get("verdict", r.get("verdict", "unmet"))
            ),
            "evidence": item.get("evidence", r.get("evidence", "")),
        })
    return repaired


def _format_script_failure(script_name: str, exc: Exception) -> dict:
    """Map a script execution exception to a structured failure record.

    Shared by ``cmd_issue`` and ``cmd_project`` (SA-0MSL1Z67Z001ZO87): each
    command keeps a tiny first-failure-only wrapper, but the reason mapping
    lives in exactly one place. Maps ``TimeoutExpired`` to a readable
    timeout reason and ``FileNotFoundError`` to the missing filename; other
    exceptions keep their str() as the reason.
    """
    reason = str(exc)
    if isinstance(exc, subprocess.TimeoutExpired):
        reason = f"Timeout after {exc.timeout}s"
    elif isinstance(exc, FileNotFoundError):
        reason = f"File not found: {exc.filename}"
    return {
        "script_name": script_name,
        "reason": reason,
        "stderr": str(exc),
    }


@dataclass
class _AuditContext:
    """Mutable state shared across the decomposed cmd_issue phases.

    Immutable inputs are set at construction; the phase functions
    (SA-0MSL1ZB5J005ENLI) read them via local aliases and write results
    back into the fields below so the next phase (and the terminal
    lifecycle) sees them. ``record_script_failure`` replaces the former
    nonlocal ``_record_script_failure`` closure (first failure wins).
    """

    issue_id: str
    persist: bool
    timeout: int | None
    parent_timeout: int | None
    pi_bin: str
    model: str | None
    model_source: str
    runner: Runner
    json_mode: bool
    debug_log: str | None
    force: bool
    worklog_dir: str | None
    batch_phase2: bool
    green_run: str | None
    audit_children: bool
    max_child_audits: int | None
    run_tests: bool
    max_citations_per_ac: int | None = None

    # Resolved / gate-phase state (set by _phase_gate)
    owning_root: str | None = None
    resolved_model: str = DEFAULT_MODEL
    green_run_block: str | None = None
    green_run_sha: str | None = None
    auto_green_run_sha: str | None = None
    test_skill_run_sha: str | None = None
    original_status: str = "open"
    original_stage: str = ""
    wi: dict | None = None
    script_failure: dict | None = None

    # Pipeline state
    work_item: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    description: str = ""
    acs: list = field(default_factory=list)
    content_fingerprint: str | None = None
    cq_findings: list = field(default_factory=list)
    cq_fixes_applied: int = 0
    cq_skipped_reason: str | None = None
    fp_screen_results: list = field(default_factory=list)
    remediation_results: dict = field(default_factory=dict)
    ac_results: list = field(default_factory=list)
    child_results: list = field(default_factory=list)
    ac_fallback_used: threading.Event = field(default_factory=threading.Event)
    phase2_completed: bool = False
    phase2_skip_note: str | None = None
    child_persist_results: list = field(default_factory=list)
    audit_verdict: str | None = None
    audit_completed: bool = False

    def record_script_failure(self, script_name: str, exc: Exception) -> None:
        """Record a script execution failure (first failure wins)."""
        if self.script_failure is not None:
            return
        self.script_failure = _format_script_failure(script_name, exc)


def _phase_gate(ctx: _AuditContext) -> int | None:
    """Phase 1 — launch-context guard, model/green-run resolution, original
    status capture, freshness + pre-flight gates, and the in_progress claim.

    Returns an exit code for early-abort paths (fresh-skip, cache-miss,
    pre-flight refusal) — cmd_issue returns it directly without touching the
    status lifecycle. Returns None when the pipeline should continue.
    """
    issue_id = ctx.issue_id
    json_mode = ctx.json_mode
    runner = ctx.runner
    worklog_dir = ctx.worklog_dir
    force = ctx.force
    green_run = ctx.green_run
    run_tests = ctx.run_tests
    model = ctx.model
    model_source = ctx.model_source

    launch_error = _verify_launch_context(issue_id, worklog_dir=worklog_dir)
    if launch_error:
        if json_mode:
            print(json.dumps({"error": launch_error}, indent=2))
        else:
            print(f"Error: {launch_error}", file=sys.stderr)
        return 1

    # Owning project root used by the Phase 1/2 FILE SCOPE manifest
    # validation and as the git root for all runner-based git commands
    # (resolved once; None → abort per AC2 — see below).
    owning_root = _resolve_owning_project_root(issue_id, worklog_dir=worklog_dir)
    if owning_root is None:
        # AC2 (SA-0MSLLGDW00098UCC): undeterminable ownership aborts. The
        # git-derived content (file-scope manifest, HEAD sha, working-tree
        # hash, green-run evidence) must resolve against the OWNING
        # project's repository; with no --worklog-dir, an unknown prefix,
        # and no sibling match, falling back to the launch cwd's repo would
        # silently scope the audit to the wrong repository. Refuse.
        error = (
            f"Undeterminable project scope for work item {issue_id}: no "
            f"--worklog-dir, unknown item prefix, and no sibling match. "
            f"Refusing to fall back to the launch cwd's repository for "
            f"git-derived content. Re-launch from the owning project or "
            f"pass --worklog-dir."
        )
        if json_mode:
            print(json.dumps({"error": error}, indent=2))
        else:
            print(f"Error: {error}", file=sys.stderr)
        return 1

    # Resolve the effective model from config + CLI
    config = _load_config()
    resolved_model = _resolve_model_for_phase(
        AUDIT_PHASE, config, model_source, cli_model=model,
    )

    if runner is None:
        runner = _default_runner

    # Resolve the git root for every runner-based git command
    # (SA-0MSLLGDW00098UCC): git-derived content must come from the OWNING
    # project's repository — never the launch cwd's. The owning root is the
    # default git root; when the launch cwd is the same git repository (a
    # worktree of the owning project, or the owning project itself), keep
    # the launch cwd so a worktree launch still resolves git to the
    # worktree checkout (worktree branch HEAD and worktree-only changes
    # stay correct — AC3). Then wrap the runner so every git call carries
    # `git -C <git_root>` when git_root differs from the launch root (AC1)
    # and stays byte-identical otherwise (zero regression for owning
    # launches).
    git_root = owning_root
    if _same_git_repository(TARGET_PROJECT_ROOT, owning_root):
        git_root = TARGET_PROJECT_ROOT
    runner = _cwd_aware_runner(git_root, runner)
    ctx.runner = runner

    # Resolve the operator-attested green test run (if any). The attestation
    # is external evidence: the runner NEVER executes the test suite itself
    # (the read-only mandate otherwise remains in force).
    green_run_block, green_run_sha = _resolve_green_run_attestation(
        green_run, runner,
    )

    # Automatic full-suite verification (SA-0MSIU5HFI0024D7W): when there is
    # no operator attestation, consume a green full-suite run from the per-repo
    # test cache READ-ONLY (query_cached never executes anything). Fail-closed:
    # a cache miss, non-zero run, or cache error yields NO evidence and the run
    # proceeds with execution-dependent ACs staying partial. The automatic path
    # augments the operator path (AC7) — a valid --green-run takes precedence.
    auto_green_run_sha = None
    test_skill_run_sha = None
    if green_run_sha is None:
        auto_block, auto_sha = _resolve_auto_green_run(
            runner, cwd=str(TARGET_PROJECT_ROOT),
        )
        if auto_sha is not None:
            green_run_block = auto_block
            auto_green_run_sha = auto_sha
        elif run_tests:
            # --run-tests path (SA-0MSJELSWS002UF60): no operator attestation
            # and no cached green evidence. Invoke the test skill (run_tests.py)
            # to execute the full project test suite in quiet mode, triage any
            # failures per the test skill, and refresh the per-repo cache so
            # subsequent audits auto-verify. This is an explicit,
            # operator-authorized deviation from the audit's read-only mandate;
            # the default (no --run-tests) stays strictly read-only.
            head_sha = _resolve_audited_head(runner)
            test_run = _run_tests_via_test_skill(
                cwd=TARGET_PROJECT_ROOT,
                parent_work_item_id=issue_id,
                head_sha=head_sha,
            )
            if test_run["success"] and head_sha is not None:
                green_run_block = _test_skill_run_prompt_block(head_sha)
                test_skill_run_sha = head_sha

    # ------------------------------------------------------------------
    # Capture original status + stage before the freshness gate / status
    # lifecycle, so the finally block can restore a safe consistent state
    # on failure and apply a verdict-driven terminal transition on success.
    # The fetched work item is also reused by the freshness gate below
    # (SA-0MSL1Z7E9005TLBA) — a single ``wl show`` serves both the status
    # capture and the freshness fingerprint/time-gate computation.
    # ------------------------------------------------------------------
    original_status = "open"  # safe default
    original_stage = ""       # safe default (unknown)
    wi: dict | None = None
    try:
        item_data = _run_wl(runner, ["wl", "show", issue_id, "--json"],
                            worklog_dir=worklog_dir)
        if isinstance(item_data, dict):
            # wl show nests the item under "workItem" — unwrap it so the
            # captured state reflects the item (not the response envelope).
            wi = item_data.get("workItem")
            if not isinstance(wi, dict):
                wi = item_data
            original_status = wi.get("status", "open")
            original_stage = wi.get("stage", "")
    except RuntimeError:
        pass  # Fall back to safe defaults

    # ------------------------------------------------------------------
    # Freshness gate: skip if a recent audit already exists
    # (before status lifecycle to avoid unnecessary in_progress transitions)
    # ------------------------------------------------------------------
    if not force:
        fresh_report = _check_audit_freshness(runner, issue_id,
                                              worklog_dir=worklog_dir,
                                              work_item=wi)
        if fresh_report is not None:
            print("Skipping: audit still fresh")
            print(fresh_report)
            return 0

    # ------------------------------------------------------------------
    # Pre-flight full-suite cache gate (SA-0MSQ72BVV0011SRU): a MISSING
    # full-suite cache at HEAD (no green cached run via query_cached) with
    # no explicit opt-out (--run-tests executes the suite; --green-run
    # attests it) blocks the audit BEFORE any Phase 1 pi call, report, or
    # persistence — and before the status lifecycle, so pre-audit
    # status/stage are preserved. Without the gate a cache miss degraded to
    # a diagnostic and the run continued, letting the Phase 2 model mark
    # execution-dependent ACs 'met' from implementer-reported evidence with
    # no forcing function to populate the cache (the degraded-verdict bug
    # this gate fixes). A red cached run and cache errors keep the
    # historical partial + diagnostic behavior (fail-open); only a miss
    # exits non-zero, and only for repos whose effective suite set actually
    # requires the suite (no-pytest repos are never falsely blocked, AC3).
    # ------------------------------------------------------------------
    if (green_run_sha is None and auto_green_run_sha is None
            and test_skill_run_sha is None and not run_tests):
        gate_message = _preflight_cache_gate(
            runner, cwd=str(TARGET_PROJECT_ROOT),
        )
        if gate_message is not None:
            if json_mode:
                print(json.dumps({"error": gate_message}, indent=2))
            else:
                print(f"Error: {gate_message}", file=sys.stderr)
            return 1

    # Track script execution failures for prominent surfacing: the
    # first-failure-only recorder lives on the context (record_script_failure)
    # so later phases and the terminal lifecycle can read ctx.script_failure.
    ctx.script_failure = None

    # Audit verdict parsed from the assembled report ("yes"/"no"); stays
    # None when the audit failed or the verdict could not be parsed.
    audit_verdict: str | None = None
    # Set True only when the audit pipeline reached a successful completion
    # (report assembled and, when persisting, persisted + read back).
    audit_completed: bool = False
    # Pre-initialized verdict containers so the finally block can scan AC
    # evidence for infra-failure markers even on early-exit paths (e.g. a
    # `wl show --children` failure that returns before ac_results is built).
    ac_results: list[dict] = []
    child_results: list[dict] = []

    # Infra-failure provenance flag (SA-0MSG9SLGI002OF7V): set whenever any
    # parent/child AC evaluation falls back to a diagnostic 'partial'
    # because of an infrastructure failure (concurrency-limit timeout,
    # provider error, unparseable Pi output, Phase-2 deep-analysis timeout).
    # A "Ready to close: No" produced solely from such fallbacks must
    # restore the pre-audit status/stage instead of demoting the item.
    # threading.Event is thread-safe: child AC screening and Phase-2 child
    # deep analysis run under a ThreadPoolExecutor.
    ac_fallback_used = threading.Event()

    # ------------------------------------------------------------------
    # Pre-flight affirmation guard (SA-0MSL1Z1WU005O5IY): the SKILL.md
    # documented a "pre-flight affirmation" that no code implemented. A
    # work item already ``in_progress`` at entry means another audit (or an
    # implementation claim) owns the item — starting a competing audit races
    # status transitions and lets the last writer win on persist + status.
    # Abort with a clear, actionable message unless ``--force`` explicitly
    # overrides the guard. No report is produced, nothing is persisted, and
    # the pre-audit status/stage are untouched (guard runs before the
    # status lifecycle below).
    # ------------------------------------------------------------------
    if not force and original_status in ("in_progress", "in-progress"):
        pre_flight_msg = (
            f"Refusing to audit {issue_id}: the work item is already "
            f"in_progress (a concurrent audit or implementation owns it). "
            f"Pass --force to bypass this pre-flight guard."
        )
        if json_mode:
            print(json.dumps({
                "error": pre_flight_msg,
                "pre_flight": {
                    "issue_id": issue_id,
                    "status": original_status,
                    "bypass": "--force",
                },
            }, indent=2))
        else:
            print(f"Error: {pre_flight_msg}", file=sys.stderr)
        return 1

    # ------------------------------------------------------------------
    # Status lifecycle: set in_progress on entry (verdict-driven on exit)
    # ------------------------------------------------------------------
    _run_wl(runner, ["wl", "update", issue_id, "--status", "in_progress", "--json"],
            worklog_dir=worklog_dir)

    # Sync the resolved gate state back into the context for later phases.
    ctx.owning_root = owning_root
    ctx.resolved_model = resolved_model
    ctx.green_run_block = green_run_block
    ctx.green_run_sha = green_run_sha
    ctx.auto_green_run_sha = auto_green_run_sha
    ctx.test_skill_run_sha = test_skill_run_sha
    ctx.original_status = original_status
    ctx.original_stage = original_stage
    ctx.wi = wi
    ctx.ac_fallback_used = ac_fallback_used
    ctx.ac_results = ac_results
    ctx.child_results = child_results
    ctx.audit_verdict = audit_verdict
    ctx.audit_completed = audit_completed
    return None


def _parse_fp_screen_response(raw_text: str,
                              findings: list[dict]) -> tuple[list[dict], bool]:
    """Parse a batched false-positive screen response into classifications.

    Returns ``(entries, failed)`` where *entries* is one dict per ruff
    finding (in input order) and *failed* is True when the whole response
    degraded (unparseable output or an infra-failure marker) — in which
    case EVERY finding is ``uncertain`` (T1 AC2).

    Entry schema:
      ``{"index": int, "finding": dict, "classification": str,
        "justification": str, "remediable": bool, "screen_failed": bool}``

    Caution-first rules:
      - A finding missing from the batch defaults to ``uncertain`` and is
        never ``confident-false-positive`` (T1 AC1).
      - A classification outside ``FP_SCREEN_VALID_CLASSIFICATIONS``
        normalizes to ``uncertain`` with a written justification.
      - Only blocking-severity (critical/high) ``confident-false-positive``
        findings are marked remediable; remediation is F2 scope (T1 AC5).
    """
    entries: list[dict] = []
    raw_text = raw_text or ""
    batch: list | None = _extract_json_array(raw_text)
    if batch is None:
        try:
            batch = json.loads(raw_text)
        except (json.JSONDecodeError, TypeError):
            batch = None

    if not isinstance(batch, list):
        # Unparseable output — degrade EVERY finding to uncertain.
        for i, f in enumerate(findings):
            entries.append({
                "index": i,
                "finding": f,
                "classification": "uncertain",
                "justification": FP_SCREEN_FAILED_JUSTIFICATION,
                "remediable": False,
                "screen_failed": True,
            })
        return entries, True

    reviewed = {
        item.get("index"): item
        for item in batch
        if isinstance(item, dict) and isinstance(item.get("index"), int)
    }
    if not reviewed and batch and all(isinstance(item, dict) for item in batch):
        # Model omitted explicit indexes — fall back to positional matching
        # (same-count arrays), keeping the caution-first rules intact.
        reviewed = {i: item for i, item in enumerate(batch)}
    for i, f in enumerate(findings):
        item = reviewed.get(i, {})
        classification = item.get("classification", "uncertain")
        if classification not in FP_SCREEN_VALID_CLASSIFICATIONS:
            classification = "uncertain"
        if not isinstance(item, dict) or "classification" not in item:
            justification = FP_SCREEN_MISSING_JUSTIFICATION
        elif item.get("classification") not in FP_SCREEN_VALID_CLASSIFICATIONS:
            justification = (
                f"Classification {item.get('classification')!r} not recognized — "
                "normalized to uncertain (caution-first)."
            )
        else:
            justification = item.get("justification", "") or ""
        remediable = (
            classification == "confident-false-positive"
            and f.get("severity") in ("critical", "high")
        )
        entries.append({
            "index": i,
            "finding": f,
            "classification": classification,
            "justification": justification,
            "remediable": remediable,
            "screen_failed": False,
        })
    return entries, False


def _fp_classification_for(finding: dict, fp_screen_results: list[dict]) -> str | None:
    """Return the screen classification for *finding*, or None when the
    finding was not screened (non-ruff finding, or screen skipped)."""
    for entry in fp_screen_results or []:
        if entry.get("finding") is finding or entry.get("finding") == finding:
            return entry.get("classification")
    return None


def _screen_ruff_findings(issue_id: str, findings: list[dict],
                          pi_bin: str, resolved_model: str,
                          debug_log: str | None, timeout: int | None,
                          ac_fallback_used: threading.Event) -> list[dict]:
    """Model-judged false-positive screen over ruff findings (F1 scope).

    Classifies each ruff finding via a SINGLE batched Pi call
    (``FP_SCREEN_CONTEXT``); non-ruff findings are never sent to the screen.
    Returns one entry per ruff finding (see ``_parse_fp_screen_response``
    for the schema) or ``[]`` when there are no ruff findings — the screen
    is skipped entirely, so zero Pi calls happen (T1 AC3).

    Caution-first degradation (T1 AC2): a provider error, timeout,
    concurrency-limit marker, unparseable output, or RuntimeError from the
    Pi call marks EVERY finding ``uncertain`` (never
    ``confident-false-positive``) and sets ``ac_fallback_used`` so a
    verdict derived from the failed screen restores the pre-audit state
    instead of demoting.
    """
    ruff_findings = [f for f in (findings or []) if f.get("linter") == "ruff"]
    if not ruff_findings:
        return []

    finding_list_json = json.dumps([
        {
            "index": i,
            "file": f.get("file", "?"),
            "line": f.get("line", 0),
            "severity": f.get("severity", "?"),
            "code": f.get("code", "?"),
            "message": f.get("message", ""),
        }
        for i, f in enumerate(ruff_findings)
    ])
    prompt = (
        "[READ-ONLY AUDIT] You are performing a read-only audit. "
        "Do NOT close, modify, or delete any work items; you MAY create "
        "a chore work item to track a false-positive finding (config-fix "
        "remediation). "
        "Do NOT execute any wl, git, or other state-modifying commands. "
        "Return ONLY a structured JSON array.\n\n"
        "Classify each ruff lint finding below as either 'genuine' (a real "
        "defect that should stay), 'confident-false-positive' (the rule "
        "misfires for this file and the finding is not a real defect), or "
        "'uncertain'. Err on the side of caution: when in doubt, choose "
        "'uncertain'.\n\n"
        "Return ONLY a JSON array of objects, each with keys 'index' "
        "(integer, matching the input), 'classification' (one of: "
        "genuine, confident-false-positive, uncertain) and 'justification' "
        "(a one-line written reason).\n\n"
        f"Findings: {finding_list_json}"
    )
    try:
        result = _call_pi_and_maybe_log(
            issue_id, FP_SCREEN_CONTEXT, prompt, model=resolved_model,
            pi_bin=pi_bin, debug_log=debug_log, timeout=timeout,
            ac_fallback_used=ac_fallback_used, child_screen=True,
        )
    except RuntimeError as exc:
        ac_fallback_used.set()
        print(
            f"Warning: Pi call failed for false-positive screen: {exc} — "
            "all findings defaulted to uncertain (caution-first).",
            file=sys.stderr,
        )
        entries, _ = _parse_fp_screen_response("", ruff_findings)
        return entries

    degraded = bool(
        result.get("_provider_error")
        or result.get("_timeout")
        or result.get("_concurrency_timeout")
    )
    if degraded:
        # Infra failure: _call_pi already set ac_fallback_used for
        # timeout/concurrency/provider-error paths; belt-and-suspenders here.
        ac_fallback_used.set()
        print(
            "Warning: false-positive screen degraded (provider error / timeout / "
            "concurrency limit) — all findings defaulted to uncertain "
            "(caution-first).",
            file=sys.stderr,
        )
        entries, _ = _parse_fp_screen_response("", ruff_findings)
        return entries

    raw_text = result.get("extracted_text", "") or result.get("evidence", "") or ""
    entries, failed = _parse_fp_screen_response(raw_text, ruff_findings)
    if failed:
        ac_fallback_used.set()
        print(
            "Warning: unparseable Pi output for false-positive screen — "
            "all findings defaulted to uncertain (caution-first).",
            file=sys.stderr,
        )
    return entries


def _effective_blocking_findings(cq_findings: list[dict],
                                 fp_screen_results: list[dict]) -> list[dict]:
    """Findings that still block closure after the false-positive screen.

    Only ``confident-false-positive`` critical/high findings are screened
    out; ``uncertain`` (caution-first) and ``genuine`` findings remain
    blocking (SA-0MST01O4G002VPBR AC4). Non-ruff findings are never
    screened and always block at critical/high.
    """
    blocking = []
    for f in cq_findings or []:
        if f.get("severity") not in ("critical", "high"):
            continue
        if _fp_classification_for(f, fp_screen_results or []) == "confident-false-positive":
            continue
        blocking.append(f)
    return blocking


def _default_max_remediation_iterations() -> int:
    """Resolve the config-fix iteration cap (env-configurable, T2 AC5).

    ``AUDIT_REMEDIATION_MAX_ITERATIONS`` overrides the default 3; invalid
    values (non-int, zero, negative) fail closed to the default.
    """
    try:
        value = int(os.environ.get(AUDIT_REMEDIATION_MAX_ITERATIONS_ENV, ""))
    except (TypeError, ValueError):
        return REMEDIATION_MAX_ITERATIONS_DEFAULT
    return value if value > 0 else REMEDIATION_MAX_ITERATIONS_DEFAULT


def _commit_config_remediation(runner: Runner, config_path: Path,
                               project_root: Path,
                               issue_id: str | None = None) -> str | None:
    """Commit the applied ruff config fix locally (no push). T2 AC3 / F2 AC3.

    Stages only the config file and commits with
    ``REMEDIATION_COMMIT_MESSAGE`` (plus the work-item id when known so
    the message references the work item — F2 AC3). Returns the short
    commit sha, or None when the commit failed (nothing to commit, git
    error).
    """
    try:
        rel = config_path.relative_to(project_root)
    except ValueError:
        rel = config_path
    message = REMEDIATION_COMMIT_MESSAGE
    if issue_id:
        message = f"{message} — {issue_id}"
    try:
        runner(["git", "add", "--", str(rel)])
        proc = runner(["git", "commit", "-m", message])
        if getattr(proc, "returncode", 0) != 0:
            return None
        head = runner(["git", "rev-parse", "--short", "HEAD"])
        sha = getattr(head, "stdout", "").strip()
        return sha or None
    except Exception:  # noqa: BLE001 -- remediation must never crash the audit
        return None


def _fp_remediation_change_summary(targets: list[dict]) -> str:
    """Human-readable summary of the per-file-ignores change applied.

    Renders ``file -> code1, code2`` for each flagged file in the
    remediated target set (F2: per-iteration config-change note).
    """
    by_file: dict[str, list[str]] = {}
    for t in targets or []:
        finding = t.get("finding", {}) if isinstance(t, dict) else {}
        file = finding.get("file", "")
        code = finding.get("code", "")
        if file and code:
            by_file.setdefault(file, [])
            if code not in by_file[file]:
                by_file[file].append(code)
    return "; ".join(
        f"{f} -> {', '.join(sorted(codes))}" for f, codes in by_file.items()
    ) or "per-file-ignores"


def _create_chore_item(runner: Runner, *, title: str, description: str,
                       worklog_dir: str | None) -> str | None:
    """Create a ``chore`` work item via ``wl create`` (F3 scope).

    Dispatches through ``_run_wl`` so ``--worklog-dir`` resolves via
    ``_resolve_worklog_flags`` (explicit dir / prefix-to-sibling — sibling
    project audits create the chore in the owning project's worklog,
    SA-0MST01PBJ008100Y AC5). Returns the new work-item id, or ``None``
    when creation failed — the caller handles the failure fail-safely
    (F3 AC5: the remediation commit still stands, the finding stays
    blocking 'genuine', the failure is recorded in the report).
    """
    cmd = ["wl", "create", "--issue-type", "chore",
           "--title", title, "--description", description, "--json"]
    try:
        data = _run_wl(runner, cmd, worklog_dir=worklog_dir)
    except RuntimeError as exc:
        print(f"Warning: chore-item creation failed: {exc}", file=sys.stderr)
        return None
    if not isinstance(data, dict) or data.get("success") is False:
        return None
    wi = data.get("workItem", {})
    if not isinstance(wi, dict):
        return None
    return wi.get("id") or None


def _chore_description_for_fix(entries: list[dict], sha: str) -> str:
    """Description for a config-fix chore: links each remediated false-
    positive finding (file, rule, justification) and the local commit sha
    (F3 AC1)."""
    lines = []
    for e in entries or []:
        finding = e.get("finding", {}) if isinstance(e, dict) else {}
        lines.append(
            f"- {finding.get('code', '?')} in {finding.get('file', '?')}:"
            f"{finding.get('line', 0)} — {e.get('justification', '')}"
        )
    body = "\n".join(lines) or "(no finding details)"
    return (
        "Ruff false positives remediated by a minimal per-file-ignores "
        f"config fix.\n{body}\n\nSilenced by config commit {sha}."
    )


def _run_remediation_loop(
    issue_id: str,
    cq_findings: list[dict],
    fp_screen_results: list[dict],
    runner: Runner,
    pi_bin: str,
    resolved_model: str,
    debug_log: str | None,
    timeout: int | None,
    ac_fallback_used: threading.Event,
    project_root: Path,
    worklog_dir: str | None,
    work_item: dict,
    content_fingerprint: str | None,
) -> dict:
    """Confident-false-positive config remediation loop (F2 scope).

    For each remediable (confident-false-positive critical/high) ruff
    finding the loop: applies a MINIMAL per-file-ignores config edit
    (``apply_ruff_remediation``), commits it locally (no push), recomputes
    the content fingerprint AFTER the commit (working tree clean), re-runs
    the scoped code-quality scan ONLY (``fix=False``, same changed-file
    scoping — the pipeline is never restarted), and re-classifies remaining
    ruff findings via the screen. Capped at
    ``_default_max_remediation_iterations()`` iterations per audit run.

    Uncertain findings never enter the loop (no config edit, no commit —
    T2 AC6). A finding still persisting after the cap is demoted to
    blocking ``genuine`` annotated ``REMEDIATION_EXHAUSTED_ANNOTATION``
    (T2 AC5).

    Returns a results dict (also the source of truth for the report):
      ``{"iterations", "max_iterations", "exhausted", "commits":
        [{"sha", "file", "fingerprint_after"}], "fingerprint_before",
        "fingerprint_after", "cq_findings", "fp_screen_results",
        "chore_items": [{"id", "commit_sha"}],
        "chore_failures": [{"commit_sha"|"finding", "error"}]}``
    """
    max_iterations = _default_max_remediation_iterations()
    results: dict = {
        "iterations": 0,
        "max_iterations": max_iterations,
        "exhausted": False,
        "commits": [],
        "chore_items": [],
        "chore_failures": [],
        "fingerprint_before": content_fingerprint,
        "fingerprint_after": content_fingerprint,
        "cq_findings": cq_findings,
        "fp_screen_results": fp_screen_results,
    }
    try:
        from skill.code_review.scripts.code_quality import run_code_quality
        from skill.code_review.scripts.linter_runner import (
            apply_ruff_remediation,
            locate_ruff_config,
        )
    except ImportError:
        # Remediation tooling unavailable — nothing to remediate.
        return results

    iteration = 0
    while iteration < max_iterations:
        targets = [
            e for e in results["fp_screen_results"] if e.get("remediable")
        ]
        if not targets:
            break
        config_path = locate_ruff_config(project_root)
        if not apply_ruff_remediation(config_path, targets):
            # Nothing to add — the config cannot silence these findings.
            results["exhausted"] = True
            break
        sha = _commit_config_remediation(runner, config_path, project_root,
                                          issue_id)
        change = _fp_remediation_change_summary(targets)
        # Track the applied config fix with a chore work item linking the
        # findings + commit sha (F3 AC1). Failure is fail-safe (F3 AC5):
        # the commit stands; the failure is recorded and the affected
        # finding stays blocking 'genuine' (post-loop pass below).
        chore_id = _create_chore_item(
            runner,
            title=f"ruff false positives silenced by config fix {sha}",
            description=_chore_description_for_fix(targets, sha),
            worklog_dir=worklog_dir,
        )
        if chore_id:
            results["chore_items"].append({
                "id": chore_id, "commit_sha": sha,
            })
        else:
            results["chore_failures"].append({
                "commit_sha": sha, "change": change,
                "error": "wl create failed",
            })
        new_fp = _compute_content_fingerprint(
            runner, issue_id, worklog_dir=worklog_dir, work_item=work_item,
        )
        results["commits"].append({
            "sha": sha,
            "file": str(config_path),
            "change": change,
            "fingerprint_after": new_fp,
        })
        # Re-run the code-quality scan ONLY (no phase re-entry): same
        # scoped changed-file list, fix=False (T2 AC4).
        cq_scope_files = _git_changed_files(runner)
        try:
            cq_result = run_code_quality(
                project_root=project_root, runner=runner, fix=False,
                files=cq_scope_files or None,
            )
            if cq_result.get("success", False):
                results["cq_findings"] = cq_result.get("findings", [])
        except Exception as exc:  # noqa: BLE001 -- scan failure keeps prior findings
            print(
                f"Warning: remediation re-scan failed: {exc} — "
                "keeping prior findings.",
                file=sys.stderr,
            )
        # Re-classify remaining ruff findings via the screen.
        results["fp_screen_results"] = _screen_ruff_findings(
            issue_id, results["cq_findings"], pi_bin, resolved_model,
            debug_log, timeout, ac_fallback_used,
        )
        results["fingerprint_after"] = new_fp
        iteration += 1

    results["iterations"] = iteration
    if (
        iteration >= max_iterations
        and any(e.get("remediable") for e in results["fp_screen_results"])
    ):
        # Cap exhaustion: the finding persists after max iterations.
        results["exhausted"] = True
    if results.get("exhausted"):
        # Persisting remediable findings become blocking 'genuine' with the
        # exhaustion annotation (T2 AC5) — never silently suppressed.
        for e in results["fp_screen_results"]:
            if e.get("remediable"):
                e["classification"] = "genuine"
                e["remediable"] = False
                e["remediation_exhausted"] = True
                e["justification"] = (
                    f"{e.get('justification', '')} — "
                    f"{REMEDIATION_EXHAUSTED_ANNOTATION}"
                ).strip(" —")

    # ── Tracked work items (F3 AC1/AC2/AC3) ────────────────────────────────
    # AC2: every medium/low confident-false-positive finding gets a
    # tracking chore (no config change, no commit link) annotated for a
    # producer decision. AC3: uncertain and genuine findings never get a
    # work item (they are not confident-false-positive).
    for e in results["fp_screen_results"]:
        finding = e.get("finding", {}) if isinstance(e, dict) else {}
        if (
            e.get("classification") == "confident-false-positive"
            and finding.get("severity") in ("medium", "low")
            and not e.get("remediation_exhausted")
        ):
            code = finding.get("code", "?")
            file = finding.get("file", "?")
            chore_id = _create_chore_item(
                runner,
                title=f"ruff {code} in {file} — {FP_CHORE_ANNOTATION}",
                description=(
                    f"False-positive candidate (medium/low severity). "
                    f"{code} in {file}:{finding.get('line', 0)} — "
                    f"{e.get('justification', '')}. {FP_CHORE_ANNOTATION}."
                ),
                worklog_dir=worklog_dir,
            )
            if chore_id:
                results["chore_items"].append({"id": chore_id})
            else:
                results["chore_failures"].append({
                    "finding": finding,
                    "error": "wl create failed",
                })

    # ── Fail-safe (F3 AC5) ────────────────────────────────────────────────
    # A failed config-fix chore leaves the affected finding blocking
    # 'genuine' (the commit stands, but the finding cannot be considered
    # tracked/resolved) — never silently suppressed. Only entries still
    # present after the re-scan are demoted; the failure itself is always
    # recorded in the report.
    failed_pairs = set()
    for f in results["chore_failures"]:
        if f.get("change"):
            for part in f["change"].split("; "):
                if " -> " in part:
                    failed_pairs.add(part)
    if failed_pairs:
        for e in results["fp_screen_results"]:
            finding = e.get("finding", {})
            key = f"{finding.get('file', '')} -> {finding.get('code', '')}"
            if key in failed_pairs and e.get("classification") != "genuine":
                e["classification"] = "genuine"
                e["remediable"] = False
                e["chore_failed"] = True
                e["justification"] = (
                    f"{e.get('justification', '')} — chore tracking failed; "
                    "finding remains blocking genuine"
                ).strip(" —")
    return results


def _phase_fetch_and_cq(ctx: _AuditContext) -> int | None:
    """Phase 2 — fetch the item + children, capture the content fingerprint,
    run the scoped code-quality scan, and extract the acceptance criteria.

    Returns 1 when the ``wl show --children`` fetch fails (a minimal failure
    report is emitted); None otherwise.
    """
    issue_id = ctx.issue_id
    json_mode = ctx.json_mode
    runner = ctx.runner
    worklog_dir = ctx.worklog_dir

    try:
        data = _run_wl(runner, ["wl", "show", issue_id, "--children", "--json"],
                       worklog_dir=worklog_dir)
    except RuntimeError as exc:
        ctx.record_script_failure("wl show", exc)
        print(f"Warning: wl show failed: {exc}", file=sys.stderr)
        # Build a minimal failure report
        fail_notice = FailureNotice(
            script_name="wl show",
            reason=str(exc),
            stderr_context=str(exc),
        )
        fail_report = fail_notice.wrap(
            f"Could not fetch work item {issue_id}. "
            "No audit report could be generated."
        )
        if json_mode:
            payload = {"error": str(exc), "script_failure": {"script_name": "wl show", "reason": str(exc)}}
            print(json.dumps(payload, indent=2))
        else:
            print(fail_report)
        return 1

    work_item = data.get("workItem", {})
    children = data.get("children", [])
    description = work_item.get("description", "")

    # Capture the content fingerprint at audit time (SA-0MSKB6US1009CNHT):
    # git HEAD sha + work-item description hash + Key Files list. The
    # fingerprint is embedded in the persisted report so a re-audit of an
    # unchanged item skips the pipeline (content-based freshness gate).
    # Fail-open: if the fingerprint cannot be computed (git unavailable,
    # wl failure), the audit proceeds and simply stores no fingerprint.
    content_fingerprint = _compute_content_fingerprint(
        runner, issue_id, worklog_dir=worklog_dir, work_item=work_item,
    )

    # ------------------------------------------------------------------
    # Code quality check (before AC verification)
    # ------------------------------------------------------------------
    cq_findings: list[dict] = []
    cq_fixes_applied: int = 0
    cq_skipped_reason: str | None = None
    try:
        from skill.code_review.scripts.code_quality import run_code_quality
        # Scoped, read-only code-quality scan (SA-0MSKB6VWU000RT58): the
        # audit lints only the git changed-file list (already computed for
        # the Phase 1/2 file-scope manifest) instead of the whole repo, and
        # never mutates files (fix=False — audits are read-only). The
        # changed-file list doubles as the scoping manifest, so no extra
        # git scan is issued here.
        cq_scope_files = _git_changed_files(runner)
        cq_result = run_code_quality(
            project_root=TARGET_PROJECT_ROOT, runner=runner, fix=False,
            files=cq_scope_files or None,
        )
        if cq_result.get("success", False):
            cq_findings = cq_result.get("findings", [])
            cq_fixes_applied = cq_result.get("fixes_applied", 0)
        else:
            cq_skipped_reason = cq_result.get("error", "Code quality check failed")
    except ImportError:
        # code_quality module not available — skip gracefully
        cq_skipped_reason = "code_quality module not available"
    except Exception as exc:  # noqa: BLE001 -- code_quality module not available, skip gracefully
        cq_skipped_reason = str(exc)

    acs = _extract_acs(description)

    # ------------------------------------------------------------------
    # False-positive screen (SA-0MST01NPD007MYG4 / SA-0MST01O4G002VPBR):
    # model-judged classification of ruff findings via a single batched Pi
    # call, BEFORE the Phase-1 blocking check consumes cq_findings. The
    # screen is skipped (zero Pi calls) when the scan yields no ruff
    # findings; non-ruff findings are never sent to it. Classifications +
    # justifications feed the Phase-1 blocking decision and the report.
    # ------------------------------------------------------------------
    try:
        fp_screen_results = _screen_ruff_findings(
            issue_id=issue_id,
            findings=cq_findings,
            pi_bin=ctx.pi_bin,
            resolved_model=ctx.resolved_model,
            debug_log=ctx.debug_log,
            timeout=ctx.timeout,
            ac_fallback_used=ctx.ac_fallback_used,
        )
    except Exception as exc:  # noqa: BLE001 -- the screen must never crash the audit
        ctx.record_script_failure("false-positive screen", exc)
        print(
            f"Warning: false-positive screen failed unexpectedly: {exc} — "
            "all findings defaulted to uncertain (caution-first).",
            file=sys.stderr,
        )
        ctx.ac_fallback_used.set()
        entries, _ = _parse_fp_screen_response("", cq_findings)
        # Consistent with _screen_ruff_findings: only ruff findings are
        # ever screened; non-ruff findings are never classified.
        fp_screen_results = [
            e for e in entries if e.get("finding", {}).get("linter") == "ruff"
        ]

    # ------------------------------------------------------------------
    # Config remediation loop (SA-0MST01OIN008MXYT / F2): confident-
    # false-positive critical/high ruff findings get a MINIMAL per-file-
    # ignores config edit, committed locally (no push), with the content
    # fingerprint re-hashed and the scoped code-quality scan re-run ONLY
    # (the pipeline is not restarted). Capped at N iterations; findings
    # persisting past the cap stay blocking genuine with the exhaustion
    # annotation. Uncertain findings never enter the loop.
    # ------------------------------------------------------------------
    remediation_results = _run_remediation_loop(
        issue_id=issue_id,
        cq_findings=cq_findings,
        fp_screen_results=fp_screen_results,
        runner=runner,
        pi_bin=ctx.pi_bin,
        resolved_model=ctx.resolved_model,
        debug_log=ctx.debug_log,
        timeout=ctx.timeout,
        ac_fallback_used=ctx.ac_fallback_used,
        project_root=TARGET_PROJECT_ROOT,
        worklog_dir=worklog_dir,
        work_item=work_item,
        content_fingerprint=content_fingerprint,
    )
    cq_findings = remediation_results.get("cq_findings", cq_findings)
    fp_screen_results = remediation_results.get(
        "fp_screen_results", fp_screen_results
    )
    content_fingerprint = remediation_results.get(
        "fingerprint_after", content_fingerprint
    )

    ctx.work_item = work_item
    ctx.children = children
    ctx.description = description
    ctx.content_fingerprint = content_fingerprint
    ctx.cq_findings = cq_findings
    ctx.cq_fixes_applied = cq_fixes_applied
    ctx.cq_skipped_reason = cq_skipped_reason
    ctx.fp_screen_results = fp_screen_results
    ctx.remediation_results = remediation_results
    ctx.acs = acs
    return None


def _phase1_parent_screening(ctx: _AuditContext) -> None:
    """Phase 3 — batched parent AC review via Pi (single call).

    Fills ctx.ac_results with one verdict entry per AC; falls back to
    'partial' diagnostics on unparseable/provider-error output (with
    infra-failure provenance set on ctx.ac_fallback_used).
    """
    issue_id = ctx.issue_id
    json_mode = ctx.json_mode
    runner = ctx.runner
    pi_bin = ctx.pi_bin
    resolved_model = ctx.resolved_model
    debug_log = ctx.debug_log
    timeout = ctx.timeout
    green_run_block = ctx.green_run_block
    owning_root = ctx.owning_root
    ac_fallback_used = ctx.ac_fallback_used
    acs = ctx.acs
    work_item = ctx.work_item
    ac_results = ctx.ac_results

    # Review parent ACs via Pi (batched into a single call for performance)
    ac_results = []
    if acs and acs[0] != "No acceptance criteria defined.":
        ac_list_json = json.dumps([{"index": i, "text": ac} for i, ac in enumerate(acs)])
        file_scope = _build_file_scope_manifest(work_item, [], runner=runner)
        # Validate the FILE SCOPE manifest covers the item repository
        # (LP-0MSQ32HNR007AI6B): a manifest built from the wrong scope
        # (e.g. the audit skill's own tree) would emit misleading
        # 'unmet' verdicts — abort with a scope error instead.
        scope_error = _validate_file_scope_manifest(file_scope, owning_root)
        if scope_error:
            if json_mode:
                print(json.dumps({"error": scope_error}, indent=2))
            else:
                print(f"Error: {scope_error}", file=sys.stderr)
            return 1
        prompt = (
            f"[READ-ONLY AUDIT] You are performing a read-only audit. "
            f"Do NOT close, modify, create, or delete any work items. "
            f"Do NOT execute any wl, git, or other state-modifying commands. "
            f"Return ONLY a structured JSON array.\n\n"
            f"Review the following acceptance criteria against the codebase. "
            f"Return ONLY a JSON array of objects, each with keys 'index' (integer), "
            f"'verdict' (one of: met, unmet, partial, adjusted) and 'evidence' "
            f"(a one-line note with file:line reference).\n\n"
            f"Evaluate criteria against user story intent and actual implementation quality, "
            f"not just literal matching of the original specification. "
            f"If a criterion has acceptable variance (implementation differs from original "
            f"spec but still satisfies user story intent), use verdict 'adjusted' instead of 'unmet'. "
            f"Include justification in the evidence field.\n\n"
            f"FILE SCOPE — Read ONLY the files listed in the manifest below. "
            f"Do not explore the whole repository (no unbounded `find`, `grep -r`, "
            f"or `ls -R` across the repo). If a criterion requires a file not listed "
            f"here, state that in the evidence instead of searching for it.\n\n"
            f"{file_scope}\n\n"
            f"{_SCANNING_BLOCK}"
            f"{green_run_block or ''}"
            f"Criteria: {ac_list_json}"
        )
        try:
            result = _call_pi_and_maybe_log(issue_id, "parent", prompt, model=resolved_model, pi_bin=pi_bin, debug_log=debug_log, enable_tools=True, timeout=timeout, ac_fallback_used=ac_fallback_used)
        except RuntimeError as exc:
            ctx.record_script_failure("pi (parent AC review)", exc)
            print(f"Warning: Pi call failed for parent AC review: {exc}", file=sys.stderr)
            result = {"verdict": "unmet", "evidence": "", "extracted_text": ""}
        # Parse the batched result - try to extract JSON array from text
        # Use extracted_text (full response) instead of evidence (may be truncated)
        raw_text = result.get("extracted_text", "") or result.get("evidence", "") or result.get("text", "")
        batch = _extract_json_array(raw_text)
        if batch is None:
            # Fallback: try direct JSON parse
            try:
                batch = json.loads(raw_text)
            except json.JSONDecodeError:
                batch = []
        if isinstance(batch, list) and batch and any(
            isinstance(item, dict) and "index" in item for item in batch
        ):
            reviewed = {item["index"]: item for item in batch if isinstance(item, dict) and "index" in item}
            for i, ac in enumerate(acs):
                item = reviewed.get(i, {})
                ac_results.append({
                    "text": ac,
                    "verdict": _normalize_verdict(item.get("verdict", "unmet")),
                    "evidence": item.get("evidence", ""),
                })
        else:
            # Fallback: this path is reached when the Pi response was not a
            # parseable JSON array. Print a warning, log raw output, and use
            # 'partial' verdict with diagnostic evidence instead of silently
            # falling back to 'unmet' with empty evidence.
            # Infra-failure provenance (SA-0MSG9SLGI002OF7V): the batch
            # JSON could not be parsed (unparseable output, provider error,
            # or concurrency-limit timeout), so any 'No' derived from these
            # verdicts must restore the pre-audit state rather than demote.
            ac_fallback_used.set()
            if result.get("_provider_error"):
                print(
                    "Warning: Pi provider error during AC evaluation — "
                    "falling back to 'partial' verdict",
                    file=sys.stderr,
                )
            else:
                print(
                    "Warning: Unparseable Pi output for AC evaluation — "
                    "falling back to 'partial' verdict",
                    file=sys.stderr,
                )
            if debug_log:
                try:
                    target = Path(debug_log) if debug_log else _default_debug_log_path(issue_id, "parent_ac_fallback")
                    _write_debug_log(target, {
                        "issue_id": issue_id,
                        "context": "parent_ac_fallback",
                        "reason": "provider_error" if result.get("_provider_error") else "parse_failure",
                        "raw_text": raw_text,
                        "result_verdict": result.get("verdict"),
                        "result_evidence": result.get("evidence", "")[:500],
                        "provider_error": result.get("_provider_error_message"),
                    })
                except Exception:  # noqa: S110, BLE001 -- optional enhancement, ignore on failure
                    pass
            # When batched parsing fails, the root-level verdict from Pi
            # cannot be trusted to represent each AC individually. Override
            # verdict to 'partial' but preserve any diagnostic evidence
            # from the root-level result (e.g., a timeout message).
            if result.get("_provider_error"):
                # Provider errors are NOT model parse failures: surface the
                # actual provider diagnostic so operators can distinguish a
                # transient model outage from a genuine unparseable response.
                provider_error = result.get("_provider_error_message", "unknown")
                evidence = (
                    f"Pi provider error: {provider_error} — "
                    "criterion could not be evaluated."
                )
            else:
                outer_evidence = result.get("evidence", "")
                if outer_evidence:
                    evidence = (
                        f"Pi model output could not be parsed — raw output logged. "
                        f"Root-level diagnostic: {outer_evidence[:500]}"
                    )
                else:
                    evidence = "Pi model output could not be parsed — raw output logged"
            for ac in acs:
                ac_results.append({
                    "text": ac,
                    "verdict": "partial",
                    "evidence": evidence,
                })
    else:
        ac_results = [{"text": "No acceptance criteria defined.", "verdict": "met", "evidence": ""}]


    ctx.ac_results = ac_results
    ctx.ac_fallback_used = ac_fallback_used


def _phase_children(ctx: _AuditContext) -> int | None:
    """Phase 4 — child orchestration + Phase 2 gate.

    Runs the ``--audit-children`` cascade or the parent-first pass-through
    flow (including the auto-trigger loop and child persistence), then the
    Phase 2 gate (blocked / no-AC / small-low-risk / full deep analysis).
    Returns 1 when child persistence is fatal; None otherwise.
    """
    runner = ctx.runner
    worklog_dir = ctx.worklog_dir
    pi_bin = ctx.pi_bin
    resolved_model = ctx.resolved_model
    model_source = ctx.model_source
    debug_log = ctx.debug_log
    timeout = ctx.timeout
    parent_timeout = ctx.parent_timeout
    persist = ctx.persist
    force = ctx.force
    audit_children = ctx.audit_children
    max_child_audits = ctx.max_child_audits
    max_citations_per_ac = _resolve_max_citations_per_ac(
        ctx.max_citations_per_ac
    )
    batch_phase2 = ctx.batch_phase2
    green_run_block = ctx.green_run_block
    owning_root = ctx.owning_root
    ac_fallback_used = ctx.ac_fallback_used
    run_tests = ctx.run_tests
    acs = ctx.acs
    work_item = ctx.work_item
    children = ctx.children
    cq_findings = ctx.cq_findings
    ac_results = ctx.ac_results
    child_results = ctx.child_results
    # Pre-initialized so the try/finally sync never hits an unbound name on
    # early-exit paths (e.g. the fatal child-persist return before the Phase 2
    # gate assigns these). Both branches reassign them later.
    phase2_completed = False
    phase2_skip_note = None
    child_persist_results = []

    # Track elapsed time so we can skip remaining child audits if we
    # approach the parent bash-tool timeout. This ensures a graceful
    # degradation instead of a silent external kill. The default guard
    # scales with the number of active children (see
    # _default_parent_timeout) so multi-child parents get a realistic
    # default budget; an explicit --parent-timeout / AUDIT_PARENT_TIMEOUT
    # override replaces the computed value entirely. The guard itself is
    # resolved below once active_children is known.
    _audit_start = time.monotonic()

    def _elapsed():
        return time.monotonic() - _audit_start

    try:
        # Review children (depth 1 only, ignore deleted)
        # Children with status=deleted (wl delete) or deletedBy (imported)
        # are excluded from active children so they don't block parent closure.
        # Pass ALL active children to the assembler; it handles the cap.
        # Note: children with status=completed/stage=done are included for
        # reporting but exempted from blocking checks by _has_phase1_blocking_issues.
        # ------------------------------------------------------------------
        # Phase 1 child AC review (P7 performance treatment).
        #
        #  1. Pre-compute each active child's persisted audit verdict BEFORE
        #     the Phase 1 child AC review so children with a fresh ready audit
        #     (child_audit_ready=True) skip the screening call and reuse the AC
        #     verdicts persisted in their own audit report.
        #  2. Pending children are reviewed with bounded parallelism
        #     (ThreadPoolExecutor capped by _resolve_parallelism()).
        #  3. The auto-trigger loop below reuses these pre-computed verdicts
        #     instead of re-querying wl audit-show per child.
        #
        # Children with status=deleted (wl delete) or deletedBy (imported)
        # are excluded from active children so they don't block parent closure.
        # Children with status=completed/stage=done are included for reporting
        # but exempted from blocking checks by _has_phase1_blocking_issues.
        # ------------------------------------------------------------------
        child_results = []
        active_children = [
            c for c in children
            if c.get("status") != "deleted" and not c.get("deletedBy")
        ]
        elapsed_guard = (
            parent_timeout if parent_timeout is not None
            else _default_parent_timeout(len(active_children))
        )

        if audit_children:
            pending_children: list[tuple[int, dict]] = []
            pre_verdicts: dict[str, tuple[bool | None, str]] = {}
            for child in active_children:
                cr = {
                    "title": child.get("title", ""),
                    "id": child.get("id", ""),
                    "status": child.get("status", ""),
                    "stage": child.get("stage", ""),
                    "effort": child.get("effort"),
                    "risk": child.get("risk"),
                }
                # Skip remaining children if we're too close to the parent
                # timeout (elapsed_guard). This prevents a silent external kill
                # and instead produces a clear diagnostic for skipped audits.
                if _elapsed() >= elapsed_guard:
                    print(
                        f"Warning: Approaching parent timeout ({_elapsed():.0f}s elapsed). "
                        f"Skipping child {child.get('id', '')} ({child.get('title', '')}). "
                        "Manual audit required for this child. Raise the budget via "
                        "--parent-timeout or AUDIT_PARENT_TIMEOUT.",
                        file=sys.stderr,
                    )
                    cr["ac_results"] = [{
                        "text": "Skipped due to audit timeout. Manual audit required.",
                        "verdict": "unmet",
                        "evidence": (
                            f"Audit runner skipped this child after "
                            f"{_elapsed():.0f}s total elapsed time to avoid "
                            f"the parent process timeout ({elapsed_guard}s "
                            f"budget; raise via --parent-timeout or "
                            f"AUDIT_PARENT_TIMEOUT). Manual audit required."
                        ),
                    }]
                    cr["child_audit_ready"] = False
                    child_results.append(cr)
                    continue

                # Completed/done children are exempt (AC5): their audits are not
                # re-checked and the Phase 1 child AC review is skipped; AC results
                # are sourced from their own persisted audit (fallback to met).
                if child.get("status") == "completed" and child.get("stage") == "done":
                    cr["child_audit_ready"] = True
                    cr["ac_results"] = _child_acs_from_own_audit(
                        child, runner, worklog_dir=worklog_dir,
                    )
                    child_results.append(cr)
                    continue

                verdict, reason, audited_at = _get_child_audit_verdict(
                    runner, child["id"], worklog_dir=worklog_dir,
                    force=force, child=child,
                )
                pre_verdicts[child["id"]] = (verdict, reason)
                if verdict is not None:
                    # Fresh valid audit (LP-0MSQ32MF200675AR child-verdict
                    # reuse): the content-fingerprint gate (primary) or the
                    # legacy time gate (fingerprint-less reports) judged the
                    # child's own audit fresh, so the parent reuses its
                    # persisted AC verdicts with ZERO pi calls — no child
                    # Phase 1 screening, no child Phase 2 deep/batch entry.
                    # Applies to ready AND not-ready verdicts (P12): the
                    # child's own pipeline already deep-analyzed these ACs.
                    cr["child_audit_ready"] = verdict is True
                    cr["child_audit_not_ready"] = verdict is False
                    cr["reused_from"] = audited_at
                    cr["ac_results"] = _child_acs_from_own_audit(
                        child, runner, worklog_dir=worklog_dir,
                    )
                else:
                    cr["child_audit_ready"] = False
                    # P12: record whether the child's own fresh audit produced an
                    # explicit 'not ready to close' verdict. Such children have
                    # already had their ACs deep-analyzed by their own audit
                    # pipeline, so the parent's Phase 2 can skip the duplicated
                    # phase2_child call (see _run_phase2_deep_analysis).
                    cr["child_audit_not_ready"] = verdict is False
                    cr["ac_results"] = []
                    pending_children.append((len(child_results), child))
                child_results.append(cr)

            # Review pending (no-audit / not-ready / not_ready) children with
            # bounded parallelism; fall back to a sequential loop for a single
            # pending child or parallelism=1 (mirrors the Phase 2 parallel pattern).
            if pending_children:
                parallelism = _resolve_child_concurrency()
                if parallelism > 1 and len(pending_children) > 1:
                    with ThreadPoolExecutor(max_workers=parallelism) as executor:
                        futures = [
                            executor.submit(
                                _phase1_review_child_acs,
                                ci, child,
                                resolved_model, pi_bin, debug_log, timeout,
                                runner, ctx.record_script_failure,
                                ac_fallback_used=ac_fallback_used,
                            )
                            for ci, child in pending_children
                        ]
                        for future in futures:
                            ci, acs = future.result()
                            child_results[ci]["ac_results"] = acs
                else:
                    for ci, child in pending_children:
                        _ci, acs = _phase1_review_child_acs(
                            ci, child,
                            resolved_model, pi_bin, debug_log, timeout,
                            runner, ctx.record_script_failure,
                            ac_fallback_used=ac_fallback_used,
                        )
                        child_results[ci]["ac_results"] = acs


            # ------------------------------------------------------------------
            # Check each active child's persisted audit verdict.
            # For children without audits or with stale audits, auto-trigger
            # a fresh audit (if persist is True AND --audit-children is set) and
            # re-evaluate. The recursive cascade is OPT-IN (SA-0MSKB6V5Q007YDHE):
            # by default no child audits are auto-triggered, so a parent with many
            # unaudited children no longer spawns an unbounded cascade. Children
            # with unchanged content are skipped via the Feature 1 content-based
            # freshness gate. Children with completed/done status+stage are
            # exempt (AC5).
            # ------------------------------------------------------------------
            _audit_runner_path = Path(__file__).resolve()
            max_child_audits = _resolve_max_child_audits(max_child_audits)
            child_audits_triggered = 0

            for child in child_results:
                # Skip completed/done children (exempt per AC5)
                if child.get("status") == "completed" and child.get("stage") == "done":
                    child["child_audit_ready"] = True  # Exempt - treat as ready
                    continue

                # Reuse the pre-computed verdict from the Phase 1 pre-pass (P7) to
                # avoid a second wl audit-show lookup per child.
                verdict, reason = pre_verdicts.get(child["id"], (None, "unknown"))

                if verdict is None and persist:
                    if _elapsed() < elapsed_guard:
                        # Content-based freshness skip (AC4): a child whose content
                        # fingerprint is unchanged has a still-valid audit — do not
                        # re-trigger it; re-evaluate the verdict from the stored
                        # report instead. --force bypasses reuse entirely: every
                        # child is re-audited (LP-0MSQ32MF200675AR AC4).
                        fresh_report = None if force else _check_audit_freshness(
                            runner, child["id"], worklog_dir=worklog_dir,
                            work_item=child,
                        )
                        if fresh_report is not None:
                            fresh_ready = _parse_ready_to_close(fresh_report)
                            verdict = fresh_ready == "yes"
                            reason = "ready" if fresh_ready == "yes" else "not_ready"
                            child["reused_from"] = _fetch_child_audited_at(
                                runner, child["id"], worklog_dir=worklog_dir,
                            )
                            print(
                                f"Reusing fresh audit for child {child['id']} "
                                f"({child['title']}) — content unchanged",
                                file=sys.stderr,
                            )
                        elif not audit_children:
                            # AC1: no cascade without explicit opt-in. The child
                            # stays not-ready (a not-ready child still blocks the
                            # parent — verdict semantics unchanged).
                            print(
                                f"Child {child['id']} ({child['title']}) has no "
                                f"fresh audit; not auto-triggering a child audit. "
                                f"Use --audit-children to enable the recursive "
                                f"cascade (or audit the child directly).",
                                file=sys.stderr,
                            )
                        elif child_audits_triggered >= max_child_audits:
                            # AC3: per-run cap reached — stop the cascade.
                            print(
                                f"Warning: child audit cap ({max_child_audits}) "
                                f"reached; not auto-auditing child {child['id']} "
                                f"({child['title']}). Raise via --max-child-audits "
                                f"or {AUDIT_MAX_CHILD_AUDITS_ENV}.",
                                file=sys.stderr,
                            )
                        else:
                            child_audits_triggered += 1
                            print(
                                f"Auto-triggering audit for child {child['id']} "
                                f"({child['title']}) — reason: {reason}",
                                file=sys.stderr,
                            )
                            try:
                                audit_cmd = [
                                    sys.executable or "python3",
                                    str(_audit_runner_path),
                                    "issue",
                                    child["id"],
                                    "--pi-bin", pi_bin,
                                    "--model", resolved_model,
                                    "--model-source", model_source,
                                    "--force",  # Bypass freshness gate
                                ]
                                if timeout is not None:
                                    audit_cmd.extend(["--timeout", str(timeout)])
                                if parent_timeout is not None:
                                    audit_cmd.extend(["--parent-timeout", str(parent_timeout)])
                                if run_tests:
                                    # Thread the operator's --run-tests authorization
                                    # into child audits so execution-dependent ACs
                                    # auto-verify there too (SA-0MSJELSWS002UF60). In
                                    # practice the parent's suite run already refreshed
                                    # the cache, so children hit it read-only.
                                    audit_cmd.append("--run-tests")
                                # Thread the resolved worklog flags through to the child
                                # runner process so it targets the same worklog store.
                                child_flags = _resolve_worklog_flags(
                                    ["wl", "show", child["id"], "--json"],
                                    explicit_dir=worklog_dir,
                                )
                                if child_flags:
                                    audit_cmd.extend(child_flags)
                                effective_timeout = CALL_PI_TIMEOUT if timeout is None else timeout
                                subprocess.run(
                                    audit_cmd,
                                    check=False,
                                    capture_output=True,
                                    text=True,
                                    timeout=effective_timeout,
                                )
                                # Re-check verdict after triggered audit
                                verdict, reason, _audited_at = _get_child_audit_verdict(
                                    runner, child["id"], worklog_dir=worklog_dir,
                                    child=child,
                                )
                            except subprocess.TimeoutExpired:
                                print(
                                    f"Warning: Auto-triggered audit for child {child['id']} "
                                    f"timed out.", file=sys.stderr,
                                )
                            except Exception as exc:  # noqa: BLE001 -- audit failure warning
                                print(
                                    f"Warning: Auto-triggered audit for child {child['id']} "
                                    f"failed: {exc}", file=sys.stderr,
                                )
                    else:
                        print(
                            f"Warning: Approaching parent timeout ({_elapsed():.0f}s elapsed). "
                            f"Cannot auto-trigger audit for child {child['id']} "
                            f"({child['title']}). Manual audit required. Raise the "
                            f"budget via --parent-timeout or AUDIT_PARENT_TIMEOUT.",
                            file=sys.stderr,
                        )

                # Set child_audit_ready: True/False if verdict is known, False otherwise
                child["child_audit_ready"] = verdict if verdict is not None else False
                # P12: record whether the child's own audit produced an explicit
                # 'not ready to close' verdict (e.g. after the auto-triggered
                # child audit above). The parent Phase 2 then skips the duplicated
                # deep-analysis call and reuses the child's own persisted findings.
                child["child_audit_not_ready"] = verdict is False

            # Initialize child_persist_results for reporting
            child_persist_results = []

            # Persist child audits to individual child work items (if persist is True)
            if persist:
                for child in child_results:
                    if child.get("reused_from"):
                        # Persistence hygiene (LP-0MSQ32MF200675AR AC5): a
                        # reused child keeps its own authoritative audit —
                        # re-persisting it would overwrite the child's own
                        # fingerprint-bearing report with a parent-style one
                        # and break future content-gate reuse.
                        continue
                    child_fingerprint = _compute_content_fingerprint(
                        runner, child["id"], worklog_dir=worklog_dir,
                    )
                    child_rc, _child_report = _persist_child_audit(
                        child_id=child["id"],
                        child_title=child["title"],
                        child_status=child["status"],
                        child_stage=child["stage"],
                        ac_results=child["ac_results"],
                        pi_bin=pi_bin,
                        model=resolved_model,
                        model_source=model_source,
                        worklog_dir=worklog_dir,
                        content_fingerprint=child_fingerprint,
                    )
                    child_persist_results.append({
                        "id": child["id"],
                        "title": child["title"],
                        "success": child_rc == 0,
                    })
                    # Child persistence failure is FATAL (LP-0MSQ32HNR007AI6B): a
                    # parent report whose child audits were never persisted is
                    # misleading — abort with a non-zero exit instead of warning.
                    if child_rc != 0 and child_rc != PERSIST_CONTENT_INVALID:
                        print(
                            f"Error: Failed to persist audit for child {child['id']} "
                            f"({child['title']}): persist_audit returned exit code "
                            f"{child_rc}. Aborting the run — the parent report "
                            f"would be misleading without the child audit.",
                            file=sys.stderr,
                        )
                        return 1
                    if child_rc == PERSIST_CONTENT_INVALID:
                        # Fallback notice WAS persisted (usable); keep the warning.
                        print(
                            f"Warning: audit for child {child['id']} "
                            f"({child['title']}) persisted with fallback content "
                            "(verdict JSON rejected); the child audit is usable.",
                            file=sys.stderr,
                        )

            # ------------------------------------------------------------------
            # Phase 2 gate: check if Phase 1 automated screening has blocking issues
            # ------------------------------------------------------------------
            phase1_blocked, phase1_reason = _has_phase1_blocking_issues(
                cq_findings, child_results, fp_screen_results=ctx.fp_screen_results
            )
            phase2_completed = False
            phase2_skip_note = None

            if phase1_blocked:
                # Phase 1 blocked → demote all "met" verdicts to "partial"
                ac_results = _demote_met_to_partial(ac_results)
                for ci, child in enumerate(child_results):
                    child_results[ci]["ac_results"] = _demote_met_to_partial(
                        child.get("ac_results", [])
                    )
                print(
                    f"Phase 1 blocked ({phase1_reason}): demoting 'met' verdicts to 'partial', "
                    "skipping Phase 2 deep analysis.",
                    file=sys.stderr,
                )
            elif not acs or acs[0] == "No acceptance criteria defined.":
                # No ACs defined — nothing to deep-analyze; skip Phase 2
                print(
                    "No acceptance criteria defined: skipping Phase 2 deep analysis.",
                    file=sys.stderr,
                )
            elif _is_low_risk_small(
                work_item.get("effort"), work_item.get("risk")
            ):
                # Small effort + Low risk — skip Phase 2 per SA-0MSQ026T3009QY2L.
                # Phase 1 verdicts stand unchanged; evidence notes the skip reason.
                # Children are evaluated independently (AC3): qualifying
                # children are dropped (and annotated) inside
                # _run_phase2_deep_analysis; non-qualifying children still get
                # deep analysis via skip_parent_deep=True.
                print(
                    f"Skipping Phase 2 deep analysis: "
                    f"effort={work_item.get('effort')}, risk={work_item.get('risk')}. "
                    f"Phase 1 verdicts stand unchanged.",
                    file=sys.stderr,
                )
                ac_results = _annotate_skip_evidence(
                    ac_results,
                    f"Phase 2 deep analysis skipped (effort={work_item.get('effort')}, "
                    f"risk={work_item.get('risk')}): small, low-risk item per "
                    f"SA-0MSQ026T3009QY2L. Phase 1 verdict stands.",
                )
                phase2_skip_note = (
                    f"small, low-risk item (effort={work_item.get('effort')}, "
                    f"risk={work_item.get('risk')}) per SA-0MSQ026T3009QY2L"
                )
                ac_results, child_results, phase2_completed = _run_phase2_deep_analysis(
                    work_item, ac_results, child_results,
                    resolved_model=resolved_model,
                    pi_bin=pi_bin,
                    debug_log=debug_log,
                    script_failure_callback=ctx.record_script_failure,
                    timeout=timeout,
                    runner=runner,
                    batch_phase2=batch_phase2,
                    worklog_dir=worklog_dir,
                    ac_fallback_used=ac_fallback_used,
                    green_run_block=green_run_block,
                    skip_parent_deep=True,
                    owning_root=owning_root,
                    max_citations_per_ac=max_citations_per_ac,
                )
                # The parent's deep analysis definitively did not run — never
                # report it as completed regardless of child outcomes.
                phase2_completed = False
            else:
                # Phase 1 passed → run Phase 2 deep code analysis
                print("Phase 1 passed: running Phase 2 deep code analysis...", file=sys.stderr)
                ac_results, child_results, phase2_completed = _run_phase2_deep_analysis(
                    work_item, ac_results, child_results,
                    resolved_model=resolved_model,
                    pi_bin=pi_bin,
                    debug_log=debug_log,
                    script_failure_callback=ctx.record_script_failure,
                    timeout=timeout,
                    runner=runner,
                    batch_phase2=batch_phase2,
                    worklog_dir=worklog_dir,
                    ac_fallback_used=ac_fallback_used,
                    green_run_block=green_run_block,
                    owning_root=owning_root,
                    max_citations_per_ac=max_citations_per_ac,
                )
        else:
            # ── PARENT-FIRST flow (default, SA-0MSKB6VJA005N43F) ──
            # Phase 1 screens parent ACs only (no child AC screening) and
            # Phase 2 parent deep analysis completes BEFORE any child audit is
            # considered. Then the parent verdict drives the child pass-through:
            #   - parent passes with no gaps → all children inherit passed
            #     (zero child audits), unless a child's own content changed
            #   - parent has gaps → only gap-mapped children are audited;
            #     unrelated children are not audited
            # --audit-children (Feature 2) forces the full per-child flow.
            phase2_completed = False
            phase2_skip_note = None
            has_blocking_cq = bool(_effective_blocking_findings(
                cq_findings, ctx.fp_screen_results
            ))
            # 1. Parent Phase 2 deep analysis FIRST (parent-only).
            if has_blocking_cq:
                # Blocking CQ findings → demote met verdicts to partial and
                # skip Phase 2 (mirrors the full-flow gate: the verdict is
                # already "Ready to close: No" via the CQ findings, so the
                # parent deep call would only burn model latency).
                ac_results = _demote_met_to_partial(ac_results)
            elif _is_low_risk_small(
                work_item.get("effort"), work_item.get("risk")
            ):
                # Small effort + Low risk — skip Phase 2 per SA-0MSQ026T3009QY2L.
                # Phase 1 verdicts stand unchanged; evidence notes the skip reason.
                print(
                    f"Skipping Phase 2 deep analysis (parent-first): "
                    f"effort={work_item.get('effort')}, risk={work_item.get('risk')}. "
                    f"Phase 1 verdicts stand unchanged.",
                    file=sys.stderr,
                )
                ac_results = _annotate_skip_evidence(
                    ac_results,
                    f"Phase 2 deep analysis skipped (effort={work_item.get('effort')}, "
                    f"risk={work_item.get('risk')}): small, low-risk item per "
                    f"SA-0MSQ026T3009QY2L. Phase 1 verdict stands.",
                )
                phase2_skip_note = (
                    f"small, low-risk item (effort={work_item.get('effort')}, "
                    f"risk={work_item.get('risk')}) per SA-0MSQ026T3009QY2L"
                )
            elif acs and acs[0] != "No acceptance criteria defined.":
                print(
                    "Phase 1 passed: running Phase 2 deep code analysis "
                    "(parent-first)...",
                    file=sys.stderr,
                )
                ac_results, _, phase2_completed = _run_phase2_deep_analysis(
                    work_item, ac_results, [],  # parent-only
                    resolved_model=resolved_model,
                    pi_bin=pi_bin,
                    debug_log=debug_log,
                    script_failure_callback=ctx.record_script_failure,
                    timeout=timeout,
                    runner=runner,
                    batch_phase2=batch_phase2,
                    worklog_dir=worklog_dir,
                    ac_fallback_used=ac_fallback_used,
                    green_run_block=green_run_block,
                    owning_root=owning_root,
                    max_citations_per_ac=max_citations_per_ac,
                )

            # 2. Parent verdict: any gaps? (unmet/partial ACs or blocking CQ).
            parent_gaps = _parent_has_gaps(ac_results) or has_blocking_cq
            gap_child_ids = set(
                _map_gaps_to_children(ac_results, active_children)
            ) if parent_gaps else set()

            # 3. Child pass-through decision.
            pending_children: list[tuple[int, dict]] = []
            for child in active_children:
                cr = {
                    "title": child.get("title", ""),
                    "id": child.get("id", ""),
                    "status": child.get("status", ""),
                    "stage": child.get("stage", ""),
                    "effort": child.get("effort"),
                    "risk": child.get("risk"),
                }
                if _elapsed() >= elapsed_guard:
                    cr["ac_results"] = [{
                        "text": "Skipped due to audit timeout. Manual audit required.",
                        "verdict": "unmet",
                        "evidence": (
                            f"Audit runner skipped this child after "
                            f"{_elapsed():.0f}s total elapsed time to avoid "
                            f"the parent process timeout ({elapsed_guard}s "
                            f"budget; raise via --parent-timeout or "
                            f"AUDIT_PARENT_TIMEOUT). Manual audit required."
                        ),
                    }]
                    cr["child_audit_ready"] = False
                    child_results.append(cr)
                    continue

                if child.get("status") == "completed" and child.get("stage") == "done":
                    cr["child_audit_ready"] = True
                    cr["ac_results"] = _child_acs_from_own_audit(
                        child, runner, worklog_dir=worklog_dir,
                    )
                    child_results.append(cr)
                    continue

                if not parent_gaps:
                    # Parent passed with no gaps → child inherits passed
                    # (AC2), unless the child's own content changed (AC6 —
                    # changed children are never silently inherited-passed).
                    if _child_content_changed(
                        runner, child["id"], worklog_dir=worklog_dir,
                        work_item=child,
                    ):
                        cr["child_audit_ready"] = False
                        pending_children.append((len(child_results), child))
                    else:
                        cr["inherited_pass"] = True
                        cr["child_audit_ready"] = True
                        cr["ac_results"] = [{
                            "text": "Inherited from parent pass",
                            "verdict": "met",
                            "evidence": (
                                "Parent audit passed with no gaps; child "
                                "inherits passed (SA-0MSKB6VJA005N43F)."
                            ),
                        }]
                elif child["id"] in gap_child_ids:
                    # Parent has gaps and this child owns the affected files
                    # → full audit (AC3).
                    cr["child_audit_ready"] = False
                    pending_children.append((len(child_results), child))
                else:
                    # Unrelated child → not audited (AC3), does not block.
                    cr["pass_through"] = "unrelated_to_gaps"
                    cr["child_audit_ready"] = True
                    cr["ac_results"] = [{
                        "text": "Not audited (unrelated to parent gaps)",
                        "verdict": "partial",
                        "evidence": (
                            "Parent audit has gaps; this child is unrelated to "
                            "the gap files and was not audited "
                            "(SA-0MSKB6VJA005N43F)."
                        ),
                    }]
                child_results.append(cr)

            # 4. Phase 1 child AC review for pending (gap-mapped / changed)
            # children only — the parent's critical path is unaffected.
            if pending_children:
                parallelism = _resolve_child_concurrency()
                if parallelism > 1 and len(pending_children) > 1:
                    with ThreadPoolExecutor(max_workers=parallelism) as executor:
                        futures = [
                            executor.submit(
                                _phase1_review_child_acs,
                                ci, child,
                                resolved_model, pi_bin, debug_log, timeout,
                                runner, ctx.record_script_failure,
                                ac_fallback_used=ac_fallback_used,
                            )
                            for ci, child in pending_children
                        ]
                        for future in futures:
                            ci, acs = future.result()
                            child_results[ci]["ac_results"] = acs
                else:
                    for ci, child in pending_children:
                        _ci, acs = _phase1_review_child_acs(
                            ci, child,
                            resolved_model, pi_bin, debug_log, timeout,
                            runner, ctx.record_script_failure,
                            ac_fallback_used=ac_fallback_used,
                        )
                        child_results[ci]["ac_results"] = acs

            # 5. Child Phase 2 deep analysis for pending children only
            # (parent already deep-verified — skip_parent_deep=True).
            if pending_children and not has_blocking_cq:
                ac_results, child_results, phase2_completed = _run_phase2_deep_analysis(
                    work_item, ac_results, child_results,
                    resolved_model=resolved_model,
                    pi_bin=pi_bin,
                    debug_log=debug_log,
                    script_failure_callback=ctx.record_script_failure,
                    timeout=timeout,
                    runner=runner,
                    batch_phase2=batch_phase2,
                    worklog_dir=worklog_dir,
                    ac_fallback_used=ac_fallback_used,
                    green_run_block=green_run_block,
                    skip_parent_deep=True,
                    owning_root=owning_root,
                    max_citations_per_ac=max_citations_per_ac,
                )

            # 6. Persist child audits (inherited children are explicit in the
            # parent report; only audited children get persisted audits).
            child_persist_results = []
            if persist:
                for child in child_results:
                    if child.get("inherited_pass") or child.get("pass_through"):
                        continue  # no per-child audit was run
                    if child.get("reused_from"):
                        # Persistence hygiene (LP-0MSQ32MF200675AR AC5): a
                        # reused child keeps its own authoritative audit —
                        # re-persisting it would overwrite the child's own
                        # fingerprint-bearing report and break future
                        # content-gate reuse.
                        continue
                    child_fingerprint = _compute_content_fingerprint(
                        runner, child["id"], worklog_dir=worklog_dir,
                    )
                    child_rc, _child_report = _persist_child_audit(
                        child_id=child["id"],
                        child_title=child["title"],
                        child_status=child["status"],
                        child_stage=child["stage"],
                        ac_results=child["ac_results"],
                        pi_bin=pi_bin,
                        model=resolved_model,
                        model_source=model_source,
                        worklog_dir=worklog_dir,
                        content_fingerprint=child_fingerprint,
                    )
                    child_persist_results.append({
                        "id": child["id"],
                        "title": child["title"],
                        "success": child_rc == 0,
                    })
                    # Child persistence failure is FATAL (LP-0MSQ32HNR007AI6B): a
                    # parent report whose child audits were never persisted is
                    # misleading — abort with a non-zero exit instead of warning.
                    if child_rc != 0 and child_rc != PERSIST_CONTENT_INVALID:
                        print(
                            f"Error: Failed to persist audit for child {child['id']} "
                            f"({child['title']}): persist_audit returned exit code "
                            f"{child_rc}. Aborting the run — the parent report "
                            f"would be misleading without the child audit.",
                            file=sys.stderr,
                        )
                        return 1
                    if child_rc == PERSIST_CONTENT_INVALID:
                        print(
                            f"Warning: audit for child {child['id']} "
                            f"({child['title']}) persisted with fallback content "
                            "(verdict JSON rejected); the child audit is usable.",
                            file=sys.stderr,
                        )


        # ------------------------------------------------------------------
    finally:
        ctx.ac_results = ac_results
        ctx.child_results = child_results
        ctx.phase2_completed = phase2_completed
        ctx.phase2_skip_note = phase2_skip_note
        ctx.child_persist_results = child_persist_results
    return None


def _phase_report(ctx: _AuditContext) -> int:
    """Phase 5 — quality epics, report assembly/output, persistence + readback.

    Sets ctx.audit_verdict and ctx.audit_completed for the terminal
    lifecycle. Returns the process exit code (0 on success, non-zero on
    persistence/readback failure).
    """
    issue_id = ctx.issue_id
    json_mode = ctx.json_mode
    runner = ctx.runner
    worklog_dir = ctx.worklog_dir
    pi_bin = ctx.pi_bin
    resolved_model = ctx.resolved_model
    model_source = ctx.model_source
    debug_log = ctx.debug_log
    timeout = ctx.timeout
    persist = ctx.persist
    ac_fallback_used = ctx.ac_fallback_used
    work_item = ctx.work_item
    cq_findings = ctx.cq_findings
    cq_fixes_applied = ctx.cq_fixes_applied
    cq_skipped_reason = ctx.cq_skipped_reason
    ac_results = ctx.ac_results
    child_results = ctx.child_results
    phase2_completed = ctx.phase2_completed
    phase2_skip_note = ctx.phase2_skip_note
    green_run_sha = ctx.green_run_sha
    auto_green_run_sha = ctx.auto_green_run_sha
    test_skill_run_sha = ctx.test_skill_run_sha
    content_fingerprint = ctx.content_fingerprint
    child_persist_results = ctx.child_persist_results
    audit_verdict = ctx.audit_verdict
    audit_completed = ctx.audit_completed

    try:
        # Create quality epics for findings (before report assembly)
        # ------------------------------------------------------------------
        if cq_findings:
            try:
                from skill.code_review.scripts.create_quality_epics import (
                    create_epics_for_findings,
                )
                _epic_result = create_epics_for_findings(cq_findings, runner=runner)
            except ImportError:
                _epic_result = {"epic_id": None, "error": "create_quality_epics module not available"}
            except Exception as exc:  # noqa: BLE001 -- epic creation failure
                _epic_result = {"epic_id": None, "error": str(exc)}

        # Assemble and output report. The assembly is reusable: the bounded
        # re-ask path (SA-0MSF3RXUB000NLOI) reassembles the report after a
        # single model re-emit of the verdict array.
        def _assemble_report() -> str:
            assembled = _assemble_issue_report(
                work_item, ac_results, child_results,
                code_quality_findings=cq_findings,
                code_quality_fixes_applied=cq_fixes_applied,
                code_quality_skipped_reason=cq_skipped_reason,
                fp_screen_results=ctx.fp_screen_results,
                remediation_results=ctx.remediation_results,
                model=resolved_model,
                model_source=model_source,
                phase2_completed=phase2_completed,
                phase2_skip_note=phase2_skip_note,
                green_run_sha=green_run_sha,
                auto_green_run_sha=auto_green_run_sha,
                test_skill_run_sha=test_skill_run_sha,
                content_fingerprint=content_fingerprint,
            )
            # Wrap report with failure notice if any subprocess calls failed
            if ctx.script_failure:
                notice = FailureNotice(
                    script_name=ctx.script_failure["script_name"],
                    reason=ctx.script_failure["reason"],
                    stderr_context=ctx.script_failure["stderr"],
                )
                assembled = notice.wrap(assembled)
            return assembled

        report = _assemble_report()

        # Capture the audit verdict for the status lifecycle transition.
        # The finally block only trusts this verdict when the audit pipeline
        # completed successfully (no script failures).
        audit_verdict = _parse_ready_to_close(report)

        if json_mode:
            payload = _build_issue_json(
                work_item, ac_results, child_results,
                code_quality_findings=cq_findings,
                code_quality_fixes_applied=cq_fixes_applied,
                fp_screen_results=ctx.fp_screen_results,
                remediation_results=ctx.remediation_results,
                phase2_completed=phase2_completed,
                phase2_skip_note=phase2_skip_note,
            )
            payload["child_persist_results"] = child_persist_results
            # Include script failure info in JSON output
            if ctx.script_failure:
                payload["script_failure"] = {
                    "script_name": ctx.script_failure["script_name"],
                    "reason": ctx.script_failure["reason"],
                    "stderr": ctx.script_failure.get("stderr", ""),
                }
            print(json.dumps(payload, indent=2))
        else:
            print(report, end="")
            # Print closing sentence (stdout UX – not persisted)
            print()
            print(_get_closing_sentence(report))

        if persist:
            persist_rc = persist_audit(issue_id, report, worklog_dir=worklog_dir)
            if persist_rc == PERSIST_CONTENT_INVALID:
                # The final persistence step rejected the assembled verdict
                # content (malformed JSON). Bounded recovery: re-ask the
                # model ONCE to re-emit the verdict array in valid JSON —
                # never re-run the full audit pipeline (SA-0MSF3RXUB000NLOI).
                repaired_acs = _reask_verdict_array_once(
                    work_item, ac_results,
                    resolved_model=resolved_model,
                    pi_bin=pi_bin,
                    debug_log=debug_log,
                    timeout=timeout,
                )
                if repaired_acs is not None:
                    ac_results = repaired_acs
                    # The bounded re-ask recovered a genuine verdict array:
                    # clear the infra-fallback flag so a genuine "No" from the
                    # repaired verdicts still demotes (SA-0MSGMR7CX00588TZ AC4).
                    ac_fallback_used.clear()
                    report = _assemble_report()
                    persist_rc = persist_audit(
                        issue_id, report, worklog_dir=worklog_dir
                    )
                if persist_rc == PERSIST_CONTENT_INVALID:
                    # persist_audit already persisted the compact fallback
                    # notice (usable, identity/readback guards pass); surface
                    # a warning instead of failing the run.
                    print(
                        f"Warning: persisted audit for {issue_id} with fallback "
                        "content — the verdict JSON could not be recovered "
                        "after the bounded re-ask.",
                        file=sys.stderr,
                    )
                    persist_rc = 0
            if persist_rc != 0:
                print(
                    f"Error: Failed to persist audit for {issue_id} "
                    f"(exit code {persist_rc})",
                    file=sys.stderr,
                )
                return persist_rc
            # Readback verification: confirm the stored audit is retrievable
            try:
                rb_data = _run_wl(runner, ["wl", "audit-show", issue_id, "--json"],
                                  worklog_dir=worklog_dir)
            except RuntimeError as exc:
                print(
                    f"Error: Readback verification failed for {issue_id}: {exc}",
                    file=sys.stderr,
                )
                return 1
            if not isinstance(rb_data, dict):
                print(
                    f"Error: Readback verification for {issue_id}: "
                    f"invalid response from wl audit-show",
                    file=sys.stderr,
                )
                return 1
            audit_obj = rb_data.get("audit")
            if audit_obj is None:
                print(
                    f"Error: Readback verification for {issue_id}: "
                    f"no audit object found",
                    file=sys.stderr,
                )
                return 1
            # Check rawOutput first, fall back to summary (some audits store
            # content in summary instead of rawOutput).
            raw_output = audit_obj.get("rawOutput")
            if not raw_output:
                raw_output = audit_obj.get("summary")
            if not raw_output:
                print(
                    f"Error: Readback verification for {issue_id}: "
                    f"stored audit is null or both rawOutput and summary are empty",
                    file=sys.stderr,
                )
                return 1
            # Content identity check (AC4): the stored audit must reference
            # the target work-item ID. This confirms the persisted audit
            # belongs to the intended item — not just that *some* non-empty
            # audit was stored (which would not catch the stale-report
            # contamination class of bug).
            if issue_id not in (raw_output or ""):
                print(
                    f"Error: Readback verification for {issue_id}: "
                    f"stored audit does not reference {issue_id}; "
                    f"suspected cross-work-item contamination",
                    file=sys.stderr,
                )
                return 1
            audit_completed = True
            return 0
        audit_completed = True
        return 0

    finally:
        ctx.ac_results = ac_results
        ctx.audit_verdict = audit_verdict
        ctx.audit_completed = audit_completed


def _apply_terminal_lifecycle(ctx: _AuditContext) -> None:
    """Phase 6 — verdict-driven terminal status transition + debug-log cleanup.

    Always runs (cmd_issue's finally): the item is never left in_progress.
    Restores the pre-audit state on failure/fallback-tainted runs; advances
    to completed/in_review only on a genuine 'Ready to close: Yes' verdict.
    """
    # ------------------------------------------------------------------
    # Status lifecycle: verdict-driven terminal transition on exit.
    # Always runs because of try/finally — the item is never left
    # in_progress after the audit completes.
    #
    #   Ready to close: Yes → completed / in_review (stage kept 'done')
    #       — even when AC evidence was fallback-tainted (WL-0MSN7XAUS008WOPQ):
    #       a fallback (e.g. a read-only test skip) does not invalidate an
    #       explicit model 'Yes' verdict; the report is complete/persisted.
    #   Ready to close: No  → open / plan_complete (only when the "No" is
    #       a genuine explicit model verdict with parseable AC evidence)
    #   Failure / timeout / unparseable verdict (infrastructure failure) →
    #       restore the captured pre-audit status+stage (fall back to
    #       open/plan_complete only when the original is unknowable) +
    #       assignee cleared so the item stays observable for a re-audit.
    #       An in_review item that hit a model timeout stays in_review —
    #       only an explicit No verdict demotes an item to open.
    #   Infra-failure fallback "No" (SA-0MSG9SLGI002OF7V): a "No"
    #       verdict produced solely from AC verdicts degraded to partial
    #       by infrastructure failure (concurrency limit, provider error,
    #       unparseable output, Phase-2 timeout) is NOT an explicit model
    #       assessment — it takes the restore branch. The flag is set at
    #       every fallback site; an evidence-marker backstop defends
    #       against future fallback sites that forget to set it. The flag
    #       only forces restore for a "No" verdict — a completed "Yes"
    #       run advances regardless (WL-0MSN7XAUS008WOPQ). If a completed
    #       "Yes" run ever takes the restore path (script failure while
    #       the report still parsed a Yes verdict), a visible warning is
    #       printed — never a silent divergence.
    #
    # The transition is retried on transient wl failures so a single
    # hiccup never leaves the item stuck in_progress; if it still fails a
    # visible warning is printed (SA-0MSAL2NQV0008HY5) instead of being
    # silently swallowed — a stuck in_progress child breaks the release
    # close step.
    # ------------------------------------------------------------------
    # Compute the intended terminal state first (no wl calls), so the
    # failure warning can tell the operator exactly what to apply.
    restore_cmd: list[str] | None = None
    # Conservative default: on any computation failure below, treat the
    # run as fallback-tainted so the debug log is retained for forensics.
    fallback_tainted = True
    try:
        # Infra-fallback provenance: a "No" derived from infrastructure-
        # failure fallbacks must restore, never demote. The flag does NOT
        # block a completed "Yes" advance (WL-0MSN7XAUS008WOPQ) — a
        # fallback on some AC evidence (e.g. a read-only test skip with a
        # variance note) leaves the overall 'Ready to close: Yes' verdict
        # trustworthy, so the item still moves to the review queue.
        fallback_tainted = ctx.ac_fallback_used.is_set()
        if ctx.audit_verdict == "no" and not fallback_tainted:
            # Evidence-marker backstop: if any AC evidence carries a known
            # infra-failure diagnostic, treat the "No" as fallback-tainted
            # even if a future fallback site forgot to set the flag.
            fallback_tainted = _evidence_has_infra_failure_markers(
                ctx.ac_results, ctx.child_results,
            )
        if (ctx.script_failure is not None or not ctx.audit_completed
                or ctx.audit_verdict is None
                or (ctx.audit_verdict == "no" and fallback_tainted)):
            # Infrastructure failure / unparseable verdict: restore the
            # pre-audit state captured on entry (status + stage). A
            # transient failure (model timeout, provider error, wl hiccup)
            # must never demote the item — an in_review item re-audited
            # during a timeout stays in_review for a re-run. Only an
            # explicit 'Ready to close: No' verdict moves an item to open
            # (SA-0MSF5PG1Y005P3AR). Falls back to open/plan_complete only
            # when the original state could not be determined (capture
            # failed / unknown). The assignee is cleared so the item stays
            # observable in the actionable queue for a re-audit.
            safe_status = ctx.original_status
            safe_stage = ctx.original_stage
            if not safe_stage:
                # Stage unknown (capture failed): pick a stage valid for
                # the restored status so wl never rejects the combo.
                safe_stage = "in_review" if safe_status == "completed" else "plan_complete"
            if ctx.audit_completed and ctx.audit_verdict == "yes":
                # Never silently diverge (WL-0MSN7XAUS008WOPQ AC4): a
                # completed run whose report parsed 'Ready to close: Yes'
                # is being restored (script failure during the run). The
                # persisted report claims closure-ready while the item
                # stays in its pre-audit state — surface that loudly so
                # the operator can review and advance manually if right.
                print(
                    f"Warning: audit for {ctx.issue_id} returned 'Ready to "
                    f"close: Yes' but the item was restored to its pre-audit "
                    f"state ({safe_status}/{safe_stage}) because of an "
                    "infrastructure failure during the run; review the "
                    "persisted report and advance the item manually if "
                    "appropriate.",
                    file=sys.stderr,
                )
            restore_cmd = [
                "wl", "update", ctx.issue_id,
                "--status", safe_status,
                "--stage", safe_stage,
                "--assignee", "",
                "--json",
            ]
        elif ctx.audit_verdict == "yes":
            # Advance to the review queue. Keep a terminal 'done' stage.
            if ctx.original_stage == "done":
                restore_cmd = ["wl", "update", ctx.issue_id, "--status", "completed", "--json"]
            else:
                restore_cmd = ["wl", "update", ctx.issue_id, "--status", "completed", "--stage", "in_review", "--json"]
        else:  # ctx.audit_verdict == "no"
            # Return to the actionable queue at a fixed pre-review stage.
            restore_cmd = ["wl", "update", ctx.issue_id, "--status", "open", "--stage", "plan_complete", "--json"]
    except RuntimeError as exc:  # pragma: no cover -- computation makes no wl calls
        print(
            f"Warning: could not compute terminal status for {ctx.issue_id}: {exc}",
            file=sys.stderr,
        )

    # ------------------------------------------------------------------
    # Debug-log lifecycle: a successful audit run removes its debug file
    # (including explicit --debug-log runs); failed or fallback-tainted
    # runs retain full-content files for forensics (SA-0MSBSOAEM0078LAO
    # AC3). A parse-failure/provider-error fallback keeps the file even
    # though the run completed, because the raw output is the only
    # forensic record of the failed evaluation.
    # ------------------------------------------------------------------
    if ctx.audit_completed and ctx.script_failure is None and not fallback_tainted:
        _remove_debug_log(ctx.debug_log, ctx.issue_id)

    if restore_cmd is not None:
        # Apply the terminal transition, retrying transient failures. The
        # update is idempotent, so retrying after a partially-applied
        # update is harmless.
        last_error: Exception | None = None
        for attempt in range(_STATUS_RESTORE_MAX_ATTEMPTS):
            try:
                _run_wl(ctx.runner, restore_cmd, worklog_dir=ctx.worklog_dir)
                last_error = None
                break
            except RuntimeError as exc:
                last_error = exc
                if attempt < _STATUS_RESTORE_MAX_ATTEMPTS - 1:
                    time.sleep(_STATUS_RESTORE_RETRY_DELAY_S * (attempt + 1))

        if last_error is not None:
            # Best-effort readback so the operator knows the item's actual
            # state (the update may have applied but the response was lost).
            actual_status = "unknown"
            try:
                rb = _run_wl(ctx.runner, ["wl", "show", ctx.issue_id, "--json"],
                             worklog_dir=ctx.worklog_dir)
                wi = rb.get("workItem") if isinstance(rb, dict) else None
                if isinstance(wi, dict):
                    actual_status = wi.get("status", "unknown")
            except RuntimeError:
                pass
            print(
                f"Warning: Failed to restore work item {ctx.issue_id} status after "
                f"audit ({last_error}); item status is '{actual_status}'. "
                f"If it was left in_progress, recover it manually, e.g. "
                f"`wl update {ctx.issue_id} --status <terminal-status> --stage <stage>`.",
                file=sys.stderr,
            )


def cmd_issue(issue_id: str, persist: bool = True,
              timeout: int | None = None,
              parent_timeout: int | None = None,
              pi_bin: str = "pi", model: str | None = None,
              model_source: str = DEFAULT_MODEL_SOURCE,
              runner: Runner | None = None, json_mode: bool = False,
              debug_log: str | None = None,
              force: bool = False,
              worklog_dir: str | None = None,
              batch_phase2: bool = False,
              green_run: str | None = None,
              audit_children: bool = False,
              max_child_audits: int | None = None,
              run_tests: bool = False,
              max_citations_per_ac: int | None = None) -> int:
    """Audit a single work item.

    The resolved model name and source are included as a metadata line
    in the audit report output (issue-level and child reports).

    Model resolution order (highest first):
      1. --model CLI flag (explicit override)
      2. Config-driven: model.audit from .ralph.json resolved via model_source
      3. Hardcoded fallback: DEFAULT_MODEL

    When *force* is ``True``, the freshness gate is bypassed and a full
    audit pipeline is always run, even if a recent audit already exists.

    *audit_children* enables the recursive child-audit cascade: when a child
    has no fresh audit, the runner auto-triggers a full child audit instead
    of leaving the child not-ready (SA-0MSKB6V5Q007YDHE). The cascade is
    OPT-IN — the default is no cascade. Children with unchanged content are
    skipped via the Feature 1 content-based freshness gate rather than
    re-audited.

    *max_child_audits* bounds the number of child audits a single run may
    auto-trigger (default: ``AUDIT_MAX_CHILD_AUDITS`` env or
    ``_DEFAULT_MAX_CHILD_AUDITS``).

    *max_citations_per_ac* bounds the file:line evidence citations per AC in
    the Phase 2 deep prompts (default: resolved via
    ``_resolve_max_citations_per_ac`` — ``--max-citations-per-ac`` CLI flag
    > ``audit.max_citations_per_ac`` CWD config key > 5). Prompt-level only
    (LP-0MSQ32WM5000NCB7).

    *worklog_dir* is an explicit ``--worklog-dir`` value that overrides
    auto-resolution for every wl call made by this run (see
    ``_resolve_worklog_flags``).

    *green_run* is the operator-attested green test run value (an exact
    commit sha or the alias ``HEAD``; resolution precedence flag > env
    ``AUDIT_GREEN_RUN`` > unset). When the value matches the audited HEAD,
    the GREEN-RUN attestation block is injected into the Phase 1 parent
    prompt and all Phase 2 prompts, and the attested sha is recorded in the
    persisted report. A mismatched or unverifiable value prints a clear
    error and the run proceeds WITHOUT the attestation (execution-dependent
    ACs stay partial) — never silently accepted.

    When no operator attestation is present, the runner attempts the
    automatic full-suite verification path (SA-0MSIU5HFI0024D7W): a green
    full-suite run is looked up READ-ONLY in the per-repo test cache
    (``query_cached`` — never executes) and, if found at the audited git
    state within the cache TTL, an AUTO-VERIFIED block is injected and the
    sha recorded as ``Automatic green run evidence``. Any miss, non-zero
    run, or cache error yields no evidence (fail-closed); the operator path
    takes precedence when both are available.

    *run_tests* (``--run-tests``, OFF by default) extends the automatic path
    (SA-0MSJELSWS002UF60): when no operator attestation exists AND the
    read-only cache holds no green full-suite run at the audited state, the
    runner invokes the test skill (``run_tests.py`` machinery) to EXECUTE
    the full project test suite in quiet mode, triage failures per the test
    skill, and refresh the per-repo cache. When the executed run is green, a
    TEST-SKILL RUN block is injected (the model MAY mark execution-dependent
    criteria met) and the sha recorded as ``Test skill run evidence``. This
    is an explicit, operator-authorized deviation from the audit's read-only
    mandate — environments that forbid test execution during audits simply
    omit the flag and behavior is unchanged (execution-dependent ACs stay
    partial with the operator instruction). A non-green executed run yields
    no evidence (fail-closed).

    For each active child (not completed/done), the child's persisted audit
    verdict is checked via ``wl audit-show``. If no audit exists or the audit
    is stale, an audit is auto-triggered for that child (via the same audit
    runner mechanism) and the resulting verdict is evaluated. A child whose
    audit says "Ready to close: No" prevents the parent from being ready to
    close. This check is performed before Phase 1 screening so that Phase 1
    can block on children not individually ready.

    Default orchestration is parent-first (SA-0MSKB6VJA005N43F): the parent
    is fully audited first (Phase 1 parent ACs + Phase 2 deep analysis) with
    no child screening; then the parent verdict drives the child pass-through
    (all children inherit passed when the parent has no gaps; only gap-mapped
    children are audited when it does). ``--audit-children`` forces the full
    per-child flow described above (explicit override).
    """

    ctx = _AuditContext(
        issue_id=issue_id, persist=persist, timeout=timeout,
        parent_timeout=parent_timeout, pi_bin=pi_bin, model=model,
        model_source=model_source, runner=runner or _default_runner,
        json_mode=json_mode, debug_log=debug_log, force=force,
        worklog_dir=worklog_dir, batch_phase2=batch_phase2,
        green_run=green_run, audit_children=audit_children,
        max_child_audits=max_child_audits, run_tests=run_tests,
        max_citations_per_ac=max_citations_per_ac,
    )
    rc = _phase_gate(ctx)
    if rc is not None:
        return rc

    try:
        rc = _phase_fetch_and_cq(ctx)
        if rc is not None:
            return rc
        _phase1_parent_screening(ctx)
        rc = _phase_children(ctx)
        if rc is not None:
            return rc
        return _phase_report(ctx)
    except AuditScopeError as exc:
        # Scope error (LP-0MSQ32HNR007AI6B): the Phase 2 FILE SCOPE
        # manifest does not cover the item repository. Fail loudly with a
        # non-zero exit; the finally block restores the pre-audit status.
        if ctx.json_mode:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    finally:
        _apply_terminal_lifecycle(ctx)


def _build_project_json(summary: str, recommendation: str) -> dict:
    """Build structured JSON payload for project-mode audit."""
    return {
        "ready_to_close": False,
        "summary": summary,
        "recommendation": recommendation,
    }


def cmd_project(timeout: int | None = None,
                pi_bin: str = "pi", model: str | None = None,
                model_source: str = DEFAULT_MODEL_SOURCE,
                runner: Runner | None = None, json_mode: bool = False,
                debug_log: str | None = None,
                worklog_dir: str | None = None,
                max_citations_per_ac: int | None = None) -> int:
    """Audit the overall project.

    Model resolution order (highest first):
      1. --model CLI flag (explicit override)
      2. Config-driven: model.audit from .ralph.json resolved via model_source
      3. Hardcoded fallback: DEFAULT_MODEL

    *worklog_dir* is an explicit ``--worklog-dir`` value that overrides
    auto-resolution for every wl call made by this run (see
    ``_resolve_worklog_flags``).

    *max_citations_per_ac* is accepted for CLI parity with the issue
    subcommand; project-level audits do not run Phase 2 deep analysis, so
    the cap is validated upstream in ``main()`` and unused here
    (LP-0MSQ32WM5000NCB7).
    """
    # Resolve the effective model from config + CLI
    config = _load_config()
    resolved_model = _resolve_model_for_phase(
        AUDIT_PHASE, config, model_source, cli_model=model,
    )

    if runner is None:
        runner = _default_runner

    # Track script execution failures
    script_failure: dict | None = None

    def _record_script_failure(script_name: str, exc: Exception) -> None:
        """Record a script execution failure into the enclosing scope.

        Only records the first failure; subsequent failures are suppressed
        to avoid overwriting the root cause. The failure record itself is
        built by the shared :func:`_format_script_failure`.
        """
        nonlocal script_failure
        if script_failure is not None:
            return
        script_failure = _format_script_failure(script_name, exc)

    try:
        # Scoped status queries only (SA-0MSLVQMKF000ESPZ): the project audit
        # needs per-status counts plus the first few blocked ids. A bare
        # `wl list --json` (5.3 MB) is replaced by three small scoped queries
        # (in-progress ~43 KB, blocked ~11 items) plus a jq-projected count
        # for completed (the OS pipe between wl and jq is unbounded, so only
        # the tiny `.count` crosses into the process buffer — the 4.9 MB
        # completed-item dump never enters memory).
        in_progress_data = _run_wl(
            runner, ["wl", "list", "--status", "in-progress", "--json"],
            worklog_dir=worklog_dir,
        )
        blocked_data = _run_wl(
            runner, ["wl", "list", "--status", "blocked", "--json"],
            worklog_dir=worklog_dir,
        )
        completed_count = _run_wl_projected(
            runner, ["wl", "list", "--status", "completed", "--json"],
            ".count", worklog_dir=worklog_dir,
        )
    except RuntimeError as exc:
        _record_script_failure("wl list", exc)
        fail_notice = FailureNotice(
            script_name="wl list",
            reason=str(exc),
            stderr_context=str(exc),
        )
        fail_report = fail_notice.wrap(
            "Could not fetch work items from Worklog. "
            "No project audit could be generated."
        )
        if json_mode:
            payload = {"error": str(exc), "script_failure": {"script_name": "wl list", "reason": str(exc)}}
            print(json.dumps(payload, indent=2))
        else:
            print(fail_report)
        return 1

    script_failure = None
    # The scoped queries return only the items with the requested status, so
    # the lists are direct (no in-memory filtering of a 5.3 MB dump needed).
    # The status filter is retained as a defensive invariant check.
    def _items(data: dict) -> list[dict]:
        items = data.get("workItems", data) if isinstance(data, dict) else data
        return items if isinstance(items, list) else []

    in_progress = [w for w in _items(in_progress_data)
                   if w.get("status") == "in_progress"]
    blocked = [w for w in _items(blocked_data)
               if w.get("status") == "blocked"]
    if not isinstance(completed_count, int):
        completed_count = 0

    summary = (
        f"Project-level audit: {len(in_progress)} items in progress, "
        f"{len(blocked)} blocked, {completed_count} completed."
    )

    if blocked:
        blocked_ids = ", ".join(w.get("id", "?") for w in blocked[:5])
        recommendation = (
            f"Review blocked items {blocked_ids} to unblock progress."
        )
    else:
        recommendation = "No specific recommendations at this time."

    # Call Pi for project-level summary. Use the model output when it is
    # parseable, otherwise fall back to the locally computed values so the
    # report is never degraded by an unparseable or failed model call
    # (SA-0MSL1YWOG005QAH8).
    pi_output_parsed = False
    prompt = (
        f"[READ-ONLY AUDIT] You are performing a read-only audit. "
        f"Do NOT close, modify, create, or delete any work items. "
        f"Do NOT execute any wl, git, or other state-modifying commands. "
        f"Return ONLY a structured JSON object.\n\n"
        f"Provide a brief project status summary based on: {summary}. "
        f"Then provide a recommendation. "
        f"Return ONLY a JSON object with keys 'summary' and 'recommendation'."
    )
    try:
        pi_result = _call_pi_and_maybe_log("project", "project", prompt, model=resolved_model, pi_bin=pi_bin, debug_log=debug_log, timeout=timeout)
        raw_text = (
            pi_result.get("extracted_text", "")
            or pi_result.get("evidence", "")
            or pi_result.get("text", "")
        )
        parsed = _extract_json_object(
            raw_text, required_keys=("summary", "recommendation")
        )
        parsed_summary = parsed.get("summary") if isinstance(parsed, dict) else None
        parsed_recommendation = parsed.get("recommendation") if isinstance(parsed, dict) else None
        if (
            isinstance(parsed_summary, str) and parsed_summary.strip()
            and isinstance(parsed_recommendation, str) and parsed_recommendation.strip()
        ):
            summary = parsed_summary.strip()
            recommendation = parsed_recommendation.strip()
            pi_output_parsed = True
        elif raw_text:
            print(
                "Warning: Unparseable Pi output for project summary — "
                "using locally computed summary/recommendation.",
                file=sys.stderr,
            )
    except RuntimeError as exc:
        _record_script_failure("pi (project-level summary)", exc)
        print(f"Warning: Pi call failed for project summary: {exc}", file=sys.stderr)

    if json_mode:
        payload = _build_project_json(summary, recommendation)
        if script_failure:
            payload["script_failure"] = {
                "script_name": script_failure["script_name"],
                "reason": script_failure["reason"],
                "stderr": script_failure.get("stderr", ""),
            }
        print(json.dumps(payload, indent=2))
    else:
        report = _assemble_project_report(summary, recommendation)
        if script_failure:
            notice = FailureNotice(
                script_name=script_failure["script_name"],
                reason=script_failure["reason"],
                stderr_context=script_failure["stderr"],
            )
            report = notice.wrap(report)
        print(report, end="")

    # Debug-log lifecycle: a successful project audit removes its debug file;
    # failed runs retain full-content files for forensics (SA-0MSBSOAEM0078LAO
    # AC3). A script failure or an unparseable Pi output keeps the file — the
    # raw output is the only forensic record of the failed evaluation.
    if script_failure is None and pi_output_parsed:
        _remove_debug_log(debug_log, "project")
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Audit runner for Worklog work items")
    sub = p.add_subparsers(dest="command")

    p_issue = sub.add_parser("issue", help="Audit a single work item")
    p_issue.add_argument("issue_id", help="Work item id to audit")
    p_issue.add_argument("--timeout", type=int, default=None,
                         help="Override the per-call Pi model timeout in seconds")
    p_issue.add_argument("--child-screen-timeout", type=int, default=None,
                         help=(
                             "Override the per-call Pi timeout for child Phase-1 "
                             "AC-review screens in seconds (default: "
                             "AUDIT_CHILD_SCREEN_TIMEOUT env or 600; lightweight "
                             "child screens fail fast instead of burning the full "
                             "1800s budget)"
                         ))
    p_issue.add_argument("--parent-timeout", type=int, default=None,
                         help=(
                             "Override the cumulative elapsed-time guard in seconds "
                             "(default: scaled by child count — " + str(PARENT_TIMEOUT_DEFAULT)
                             + "s base + " + str(PARENT_TIMEOUT_PER_CHILD)
                             + "s per active child); raise this to audit children that "
                             "would otherwise be skipped (env: AUDIT_PARENT_TIMEOUT)"
                         ))
    p_issue.add_argument("--batch-phase2", action="store_true",
                         help=(
                             "Enable Phase 2 batch deep analysis: fold the parent ACs and "
                             "pending child ACs into ONE indexed pi call (env: "
                             "AUDIT_PHASE2_BATCH; default off)"
                         ))
    p_issue.add_argument("--do-not-persist", action="store_true",
                         help="Do not persist the audit report via wl update")
    p_issue.add_argument("--pi-bin", default="pi", help="Path to the pi binary (default: pi)")
    p_issue.add_argument("--model", default=None,
                         help="Pi model to use for review (default: resolved from .ralph.json)")
    p_issue.add_argument("--model-source", default=DEFAULT_MODEL_SOURCE,
                         choices=sorted(MODEL_SOURCES),
                         help="Model source: remote or local (default: local)")
    p_issue.add_argument("--json", action="store_true",
                         help="Emit machine-readable JSON output instead of markdown")
    p_issue.add_argument("--debug-log", default=None,
                         help="Append Pi debug output to this file (JSONL)")
    p_issue.add_argument("--force", action="store_true",
                         help="Bypass the freshness gate and force a full audit")
    p_issue.add_argument("--audit-children", action="store_true",
                         help=(
                             "Opt-in: auto-trigger a full audit for each active child "
                             "that has no fresh audit (recursive cascade). Default is "
                             "NO cascade — children without fresh audits stay not-ready "
                             "and block the parent (SA-0MSKB6V5Q007YDHE)"
                         ))
    p_issue.add_argument("--max-child-audits", type=int, default=None,
                         help=(
                             "Per-run cap on auto-triggered recursive child audits "
                             f"(default: {AUDIT_MAX_CHILD_AUDITS_ENV} env or "
                             f"{_DEFAULT_MAX_CHILD_AUDITS})"
                         ))
    p_issue.add_argument("--max-citations-per-ac", type=int, default=None,
                         help=(
                             "Max file:line evidence citations per criterion in the "
                             "Phase 2 deep-analysis prompts "
                             f"(default: audit.max_citations_per_ac config key or "
                             f"{_DEFAULT_MAX_CITATIONS_PER_AC})"
                         ))
    p_issue.add_argument("--worklog-dir", default=None,
                         help="Explicit .worklog directory to target (overrides auto-resolution)")
    p_issue.add_argument("--max-concurrency", type=int, default=None,
                         help="Max concurrent pi/audit subprocesses (default: AUDIT_MAX_CONCURRENCY env or 2)")
    p_issue.add_argument("--green-run", default=None, metavar="SHA|HEAD",
                         help=(
                             "Operator-attested green test run: the full project test "
                             "suite passed at this commit. Accepts 'HEAD' (resolved to "
                             "the current HEAD sha) or an exact sha that must match the "
                             "audited HEAD. When valid, execution-dependent acceptance "
                             "criteria (e.g. 'full test suite passes') MAY be marked met "
                             "based on the attestation; the runner never executes the "
                             "suite (env: AUDIT_GREEN_RUN; flag wins)"
                         ))
    p_issue.add_argument("--run-tests", action="store_true",
                         help=(
                             "Execute the full project test suite via the test skill "
                             "(/skill:test / run_tests.py) when execution-dependent "
                             "acceptance criteria are present and no cached green "
                             "full-suite run exists, then auto-verify those criteria "
                             "from the executed green run. OFF by default: the audit "
                             "is otherwise strictly read-only and never executes the "
                             "suite (environments that forbid test execution during "
                             "audits are unaffected). Failures are triaged per the "
                             "test skill (critical test-failure work items)."
                         ))

    p_project = sub.add_parser("project", help="Audit the overall project")
    p_project.add_argument("--timeout", type=int, default=None,
                           help="Override the per-call Pi model timeout in seconds")
    p_project.add_argument("--pi-bin", default="pi", help="Path to the pi binary (default: pi)")
    p_project.add_argument("--model", default=None,
                           help="Pi model to use for review (default: resolved from .ralph.json)")
    p_project.add_argument("--model-source", default=DEFAULT_MODEL_SOURCE,
                           choices=sorted(MODEL_SOURCES),
                           help="Model source: remote or local (default: local)")
    p_project.add_argument("--json", action="store_true",
                           help="Emit machine-readable JSON output instead of markdown")
    p_project.add_argument("--debug-log", default=None,
                           help="Append Pi debug output to this file (JSONL)")
    p_project.add_argument("--worklog-dir", default=None,
                           help="Explicit .worklog directory to target (overrides auto-resolution)")
    p_project.add_argument("--max-concurrency", type=int, default=None,
                           help="Max concurrent pi/audit subprocesses (default: AUDIT_MAX_CONCURRENCY env or 2)")
    p_project.add_argument("--max-citations-per-ac", type=int, default=None,
                           help=(
                               "Accepted for CLI parity with the issue subcommand; "
                               "project-level audits do not run Phase 2 deep analysis "
                               f"(default: audit.max_citations_per_ac config key or "
                               f"{_DEFAULT_MAX_CITATIONS_PER_AC})"
                           ))

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_usage(sys.stderr)
        return 2

    # CLI --max-concurrency overrides AUDIT_MAX_CONCURRENCY for this process.
    # _call_pi reads the env var via _audit_semaphore_max_workers().
    max_concurrency = getattr(args, "max_concurrency", None)
    if max_concurrency is not None:
        os.environ[ENV_MAX_WORKERS] = str(max_concurrency)

    # CLI --child-screen-timeout overrides AUDIT_CHILD_SCREEN_TIMEOUT for this
    # process. _call_pi resolves the child Phase-1 screen budget via the env
    # var (mirrors the --max-concurrency pattern; LP-0MSQ32S2M001EA74 AC1).
    child_screen_timeout = getattr(args, "child_screen_timeout", None)
    if child_screen_timeout is not None:
        os.environ[AUDIT_CHILD_SCREEN_TIMEOUT_ENV] = str(child_screen_timeout)

    # Detect proxy 'cheap' mode before any pi call and serialize this run's
    # parallelism (AUDIT_PARALLELISM=1 + AUDIT_MAX_CONCURRENCY=1) so the
    # audit does not race the proxy's single-slot pool (SA-0MSN04X2S006ONH0).
    # Fail-open: a failed query or any other mode leaves settings unchanged.
    _apply_proxy_mode_serialization()

    if args.command == "issue":
        return cmd_issue(args.issue_id, persist=not args.do_not_persist,
                         timeout=_resolve_effective_timeout(args.timeout),
                         parent_timeout=_resolve_parent_timeout(args.parent_timeout),
                         pi_bin=args.pi_bin, model=args.model,
                         model_source=args.model_source, json_mode=args.json,
                         debug_log=args.debug_log,
                         force=args.force,
                         worklog_dir=args.worklog_dir,
                         batch_phase2=_phase2_batch_enabled(args.batch_phase2),
                         green_run=args.green_run,
                         audit_children=args.audit_children,
                         max_child_audits=_resolve_max_child_audits(
                             args.max_child_audits
                         ),
                         max_citations_per_ac=_resolve_max_citations_per_ac(
                             args.max_citations_per_ac
                         ),
                         run_tests=args.run_tests)
    elif args.command == "project":
        return cmd_project(timeout=_resolve_effective_timeout(args.timeout),
                           pi_bin=args.pi_bin, model=args.model,
                           model_source=args.model_source, json_mode=args.json,
                           debug_log=args.debug_log,
                           worklog_dir=args.worklog_dir,
                           max_citations_per_ac=_resolve_max_citations_per_ac(
                               args.max_citations_per_ac
                           ))

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
