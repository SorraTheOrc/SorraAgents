#!/usr/bin/env python3
"""Audit runner – deterministic audit orchestration.

Provides two subcommands:
  issue <id>   – audit a single work item
  project      – audit the overall project

Usage:
  audit_runner.py issue <id> [--do-not-persist] [--pi-bin pi] [--model <name>]
  audit_runner.py project [--pi-bin pi] [--model <name>]

Verdicts:
  met       – acceptance criterion fully satisfied
  unmet     – acceptance criterion not satisfied
  partial   – acceptance criterion partially satisfied
  adjusted  – acceptance criterion adapted with acceptable variance
              (does not block ready-to-close, recorded in variance decisions)

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
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.audit.scripts.persist_audit import persist_audit
from skill.scripts.failure_notice import FailureNotice
from skill.shared.process_semaphore import (
    DEFAULT_MAX_WORKERS,
    ENV_MAX_WORKERS,
    Semaphore,
)
from skill.shared.status_lifecycle import _wl_error_detail, worklog_dir_flag

# ---------------------------------------------------------------------------
# Concurrency control (fan-out bounding, SA-0MSAEKOQE009TEB4)
# ---------------------------------------------------------------------------
AUDIT_SEMAPHORE_NAME = "audit"
AUDIT_LOCK_TIMEOUT_ENV = "AUDIT_LOCK_TIMEOUT"
AUDIT_LOCK_TIMEOUT_DEFAULT = 300.0
"""Bounded wait (seconds) for a free audit concurrency slot.

Configurable via ``AUDIT_LOCK_TIMEOUT``. When the ceiling is saturated
for longer than this, the pi call returns an ``unmet`` verdict with a
clear message instead of launching yet another unbounded process.
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

The cumulative elapsed-time guard in ``cmd_issue`` (``PARENT_TIMEOUT_DEFAULT``
= 110s threshold for child audit skipping) provides the primary protection
against the parent bash-tool execution timeout (~120s), not this per-call
timeout. The guard threshold is configurable via the ``--parent-timeout`` CLI
flag or the ``AUDIT_PARENT_TIMEOUT`` environment variable (see
``_resolve_parent_timeout``) for harnesses whose bash tool allows longer runs.

If the Pi model itself takes longer than this value, something is likely
wrong (model hang, provider issue) and the timeout diagnostic should be
produced rather than blocking indefinitely.

The effective per-call timeout can be raised via the ``--timeout`` CLI flag
or the ``AUDIT_PI_TIMEOUT`` environment variable (see
``_resolve_effective_timeout``).
"""

PARENT_TIMEOUT_DEFAULT = 110
"""Default cumulative elapsed-time guard threshold (seconds) in ``cmd_issue``.

When the total elapsed time since the audit start marker exceeds this value,
remaining child audits are skipped with a clear diagnostic instead of risking
a silent kill by the parent bash-tool execution timeout (~120s).

The threshold can be raised via the ``--parent-timeout`` CLI flag or the
``AUDIT_PARENT_TIMEOUT`` environment variable (see
``_resolve_parent_timeout``) for harnesses that allow longer parent runs.
"""

AUDIT_PI_TIMEOUT_ENV = "AUDIT_PI_TIMEOUT"
"""Environment variable name for overriding the per-call Pi timeout.

When set (and ``--timeout`` is not passed), this value (seconds) is used as
the effective per-call Pi model timeout instead of ``CALL_PI_TIMEOUT``.
This lets release operators raise the audit runner's tolerance for slow
Pi model calls (e.g. ``AUDIT_PI_TIMEOUT=3600``) without changing defaults.
"""

AUDIT_PARENT_TIMEOUT_ENV = "AUDIT_PARENT_TIMEOUT"
"""Environment variable name for overriding the cumulative elapsed-time guard.

When set (and ``--parent-timeout`` is not passed), this value (seconds)
replaces ``PARENT_TIMEOUT_DEFAULT`` as the elapsed-time threshold for
skipping remaining child audits in ``cmd_issue``. This lets release
operators extend the overall audit run budget (e.g.
``AUDIT_PARENT_TIMEOUT=3600``) without changing defaults.
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

_PHASE2_MAX_RETRIES = 1
"""Provider-error retry cap for long agent-mode Phase 2 calls.

Phase 2 deep analysis (``phase2_deep`` / ``phase2_child``) runs Pi in agent
mode with file-reading tools, so each call is long. A provider error late in
such a call must not restart the entire call multiple times (worst case
~3 x 1800s before this change); retrying at most once bounds the cost while
still giving transient provider glitches a chance to recover. Short Phase 1
bare calls keep ``_PI_MAX_RETRIES`` (2).
"""

AUDIT_PHASE2_PARALLELISM_ENV = "AUDIT_PHASE2_PARALLELISM"
"""Environment variable controlling Phase 2 child deep-analysis concurrency.

When set (an integer >= 1), this is the maximum number of independent child
``phase2_child:<i>`` Pi calls that may run concurrently inside
``_run_phase2_deep_analysis``. The parent deep-analysis call always runs
first and is never parallelized. Setting this to 1 restores the historical
strictly-sequential behavior. Invalid values are ignored with a warning.
"""

_PHASE2_DEFAULT_PARALLELISM = 2
"""Default bounded concurrency cap for Phase 2 child deep-analysis calls.

Chosen conservatively so the local model proxy is not overwhelmed while
still collapsing the N-sequential-calls wall-clock to N/cap. Operators can
override via ``AUDIT_PHASE2_PARALLELISM``.
"""

AUDIT_FRESHNESS_BUFFER_SECONDS = 60
"""Freshness buffer (seconds) for the recent-audit gate.

When the audit's ``auditedAt`` timestamp is more recent than the work item's
``updatedAt`` timestamp plus this buffer, the audit is considered fresh and
the runner skips the full audit pipeline.
"""

# Verdict constants
VERDICT_MET = "met"
VERDICT_UNMET = "unmet"
VERDICT_PARTIAL = "partial"
VERDICT_ADJUSTED = "adjusted"
_ACCEPTABLE_VERDICTS = {VERDICT_MET, VERDICT_ADJUSTED}

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


def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def _extract_work_item_prefix(cmd: Sequence[str]) -> str | None:
    """Extract the work-item id prefix (e.g. ``OSL``) from a wl command.

    Returns the prefix before the first ``-`` in an argument that looks like a
    work item id (e.g. ``OSL-0MSABC7SB001NVUN``), or ``None`` when no such
    argument is present (e.g. ``wl list``).
    """
    for arg in cmd:
        m = re.match(r"^([A-Z]{2,5})-[A-Z0-9]+$", arg)
        if m:
            return m.group(1)
    return None


def _find_worklog_dir_by_prefix(prefix: str) -> Path | None:
    """Find the ``.worklog`` dir of a sibling project with matching config prefix.

    Scans ``TARGET_PROJECT_ROOT.parent/*/.worklog/config.yaml`` and returns the
    first ``.worklog`` directory whose ``prefix:`` value equals *prefix*.
    Returns ``None`` when no sibling project matches (or the scan is not
    possible, e.g. TARGET_PROJECT_ROOT.parent does not exist).
    """
    projects_root = TARGET_PROJECT_ROOT.parent
    try:
        configs = sorted(projects_root.glob("*/.worklog/config.yaml"))
    except OSError:
        return None
    for config in configs:
        try:
            text = config.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("prefix:"):
                value = stripped.split(":", 1)[1].strip().strip('"\'')
                if value == prefix:
                    return config.parent
    return None


def _resolve_worklog_flags(cmd: Sequence[str],
                           explicit_dir: str | None = None) -> list[str]:
    """Resolve ``--worklog-dir`` flags for a wl command.

    Resolution order:
      1. explicit ``--worklog-dir`` value (from CLI / caller)
      2. prefix-to-project sibling scan: extract the work-item id prefix from
         the command and match it against
         ``TARGET_PROJECT_ROOT.parent/*/.worklog/config.yaml``
      3. cwd-chain fallback (shared :func:`worklog_dir_flag`)
      4. no flag (wl resolves from cwd)
    """
    if explicit_dir:
        return ["--worklog-dir", explicit_dir]
    prefix = _extract_work_item_prefix(cmd)
    if prefix:
        wl_dir = _find_worklog_dir_by_prefix(prefix)
        if wl_dir is not None:
            return ["--worklog-dir", str(wl_dir)]
    return worklog_dir_flag()


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


# ---------------------------------------------------------------------------
# Freshness gate
# ---------------------------------------------------------------------------


def _check_audit_freshness(runner: Runner, issue_id: str,
                           worklog_dir: str | None = None) -> str | None:
    """Check if there's a fresh audit for the work item.

    Fetches the latest audit via ``wl audit-show <id> --json`` and compares
    the audit's ``auditedAt`` timestamp against the work item's ``updatedAt``
    timestamp plus ``AUDIT_FRESHNESS_BUFFER_SECONDS``.

    Returns the ``rawOutput`` of the existing audit if still fresh, ``None``
    otherwise (no prior audit, stale audit, or command failure).

    The gate gracefully falls through on any failure (no audit data, command
    error, parse error) so that the normal audit pipeline always runs when
    freshness cannot be determined.
    """
    from datetime import datetime, timedelta, timezone

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

    # Get the work item's updatedAt
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
    try:
        # Normalize Z suffix for Python 3.10 compatibility
        audit_time_str = str(audited_at).replace("Z", "+00:00")
        update_time_str = str(updated_at).replace("Z", "+00:00")

        audit_time = datetime.fromisoformat(audit_time_str)
        update_time = datetime.fromisoformat(update_time_str)

        # Ensure both are timezone-aware for comparison
        if audit_time.tzinfo is None:
            audit_time = audit_time.replace(tzinfo=timezone.utc)
        if update_time.tzinfo is None:
            update_time = update_time.replace(tzinfo=timezone.utc)

        freshness_threshold = update_time + timedelta(seconds=AUDIT_FRESHNESS_BUFFER_SECONDS)

        if audit_time > freshness_threshold:
            return raw_output
    except (ValueError, TypeError):
        return None

    return None


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


def _resolve_parent_timeout(cli_value: int | None) -> int:
    """Resolve the cumulative elapsed-time guard threshold in seconds.

    Precedence:
      1. ``--parent-timeout`` CLI flag (explicit override)
      2. ``AUDIT_PARENT_TIMEOUT`` environment variable (seconds)
      3. ``PARENT_TIMEOUT_DEFAULT`` (110s — preserves current behavior)

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
                f"using default guard ({PARENT_TIMEOUT_DEFAULT}s)",
                file=sys.stderr,
            )
    return PARENT_TIMEOUT_DEFAULT


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
    """Resolve the bounded wait for a free audit concurrency slot.

    Configurable via ``AUDIT_LOCK_TIMEOUT`` (seconds); default
    ``AUDIT_LOCK_TIMEOUT_DEFAULT`` (300). An invalid value is ignored with
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
            wait (``AUDIT_LOCK_TIMEOUT``).
    """
    sem = Semaphore(
        AUDIT_SEMAPHORE_NAME,
        max_workers=_audit_semaphore_max_workers(max_concurrency),
        timeout=_audit_lock_timeout(),
    )
    sem.acquire()
    return sem


def _resolve_phase2_parallelism() -> int:
    """Resolve the bounded concurrency cap for Phase 2 child deep analysis.
    Precedence:
      1. ``AUDIT_PHASE2_PARALLELISM`` environment variable (integer >= 1)
      2. ``_PHASE2_DEFAULT_PARALLELISM`` (2)

    Values below 1 are clamped to 1. An invalid (non-integer) value is
    ignored with a warning so a misconfigured environment cannot break the
    audit run.
    """
    env_value = os.environ.get(AUDIT_PHASE2_PARALLELISM_ENV)
    if env_value:
        try:
            parsed = int(env_value)
            return max(1, parsed)
        except ValueError:
            print(
                f"Warning: invalid {AUDIT_PHASE2_PARALLELISM_ENV} value {env_value!r}; "
                "using default parallelism",
                file=sys.stderr,
            )
    return _PHASE2_DEFAULT_PARALLELISM


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

def _call_pi(prompt: str, model: str = DEFAULT_MODEL,
             pi_bin: str = "pi",
             enable_tools: bool = False,
             timeout: int | None = None,
             max_retries: int | None = None) -> dict:
    """Call Pi via subprocess and parse the JSON-stream response.

    Args:
        prompt: The prompt text to send to Pi.
        model: The model name to use.
        pi_bin: Path to the pi binary.
        enable_tools: If True, adds ``--tools read,bash,grep,find,ls
                       --exclude-tools ask_question`` to enable file-reading
                       capabilities in the Pi agent session.
        max_retries: Maximum number of extra attempts after a provider error.
            When None, falls back to ``_PI_MAX_RETRIES`` (2). Long
            agent-mode Phase 2 calls pass 1 so a provider error does not
            restart a long call multiple times (evaluation lever P5).

    Returns a dict with keys ``verdict`` and ``evidence``.
    On success, implementations may also include additional diagnostic keys
    such as ``raw_stdout``, ``raw_stderr`` and ``extracted_text`` which are
    useful for debugging. This function returns at minimum
    ``{"verdict": <met|unmet|partial|adjusted>, "evidence": <text>}``.

    Uses the same JSON-stream protocol as ralph (``pi -p --mode json``).
    Uses ``communicate()`` to avoid pipe-buffer deadlocks.
    """
    cmd = [pi_bin, "-p", "--mode", "json", "--model", model, prompt]
    if enable_tools:
        cmd.extend([
            "--tools", "read,bash,grep,find,ls",
            "--exclude-tools", "ask_question",
        ])

    effective_timeout = CALL_PI_TIMEOUT if timeout is None else timeout
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
            # holds one audit slot; the bounded wait is AUDIT_LOCK_TIMEOUT.
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
                    stdout, stderr = process.communicate(timeout=effective_timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                    return {
                        "verdict": "unmet",
                        "evidence": (
                            f"Pi model call timed out after {effective_timeout}s. "
                            "Manual audit required."
                        ),
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
    text = _extract_pi_text(raw)
    if not text:
        return {"verdict": "unmet", "evidence": "", "raw_stdout": stdout, "raw_stderr": stderr, "elapsed_seconds": elapsed_seconds}

    # Try to parse the text as JSON with verdict/evidence
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return {
                "verdict": obj.get("verdict", "unmet").lower(),
                "evidence": obj.get("evidence", ""),
                "raw_stdout": stdout,
                "raw_stderr": stderr,
                "extracted_text": text,
                "elapsed_seconds": elapsed_seconds,
            }
    except json.JSONDecodeError:
        pass

    # If Pi returned free-form text, use it as evidence and default to met
    return {"verdict": "met", "evidence": text.strip()[:200], "raw_stdout": stdout, "raw_stderr": stderr, "extracted_text": text, "elapsed_seconds": elapsed_seconds}


def _extract_pi_text(raw: str) -> str:
    """Extract user-facing text from pi --mode json output.

    Uses the same parsing logic as ralph_loop._parse_pi_json_line.
    """
    delta_parts: list[str] = []
    complete_blocks: list[str] = []

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stream_text, _, complete_text = _parse_pi_json_line(stripped)
        if stream_text is None and complete_text is None:
            continue  # not valid JSON
        if complete_text is not None:
            complete_blocks.append(complete_text)
        elif stream_text:
            delta_parts.append(stream_text)

    # Prefer complete blocks (agent_end, text_end) over accumulated deltas
    if complete_blocks:
        return complete_blocks[-1]
    return "".join(delta_parts)


def _parse_pi_json_line(line: str):
    """Parse a single JSON line from pi --mode json.

    Returns (stream_text, should_print, complete_text), same as ralph.
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None, False, None
    if not isinstance(obj, dict):
        return None, False, None

    event_type = obj.get("type", "")

    if event_type == "message_update":
        assistant = obj.get("assistantMessageEvent")
        if isinstance(assistant, dict):
            inner = assistant.get("type", "")
            if inner == "text_delta":
                delta = assistant.get("delta", "")
                return (delta, bool(delta), None) if delta else ("", False, None)
            if inner == "text_end":
                content = assistant.get("content", "")
                return ("", False, content) if content else ("", False, None)
            if inner in ("thinking_start", "thinking_delta", "thinking_end",
                         "toolcall_start", "toolcall_delta", "toolcall_end",
                         "text_start"):
                return "", False, None
            content_text = _extract_text_from_content(assistant.get("content"))
            if content_text:
                return "", False, content_text
            return "", False, None
        return "", False, None

    if event_type in ("message_start", "message_end", "turn_end"):
        message = obj.get("message")
        # Only extract text from assistant messages. The user prompt echo
        # (message_start with role=user) must never be treated as model
        # output — it caused false parse failures when the model errored out
        # mid-stream and the prompt was the last complete text block.
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return "", False, None
        text = _extract_text_from_assistant_message(message)
        if text:
            return "", False, text
        return "", False, None

    if event_type == "agent_end":
        text = _extract_last_assistant_message_text(obj.get("messages"))
        if text:
            return "", False, text
        return "", False, None

    if event_type in ("session", "agent_start", "turn_start",
                       "tool_execution_start", "tool_execution_update",
                       "tool_execution_end"):
        return "", False, None

    # Fallback
    for key in ("content", "text", "delta"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            return val, True, None
    return "", False, None


def _extract_text_from_content(content) -> str | None:
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                t = item.get("text", "")
                if t:
                    parts.append(t)
        return "".join(parts) if parts else None
    return None


def _extract_text_from_assistant_message(message) -> str | None:
    if not isinstance(message, dict):
        return None
    return _extract_text_from_content(message.get("content"))


def _extract_last_assistant_message_text(messages) -> str | None:
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            text = _extract_text_from_content(msg.get("content"))
            if text:
                return text
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


def _extract_acs(description: str) -> list[str]:
    """Extract acceptance criteria lines from a markdown description."""
    pattern = re.compile(
        r"^#{0,3}\s*(?:Acceptance|Success)\s+Criteria:?\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    match = pattern.search(description)
    if not match:
        return ["No acceptance criteria defined."]

    start = match.end()
    lines = description[start:].splitlines()
    acs: list[str] = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^#{1,6}\s", stripped):
            break
        numbered = re.match(r"^\d+\.\s+(.*)", stripped)
        if numbered:
            acs.append(numbered.group(1))
            continue
        bulleted = re.match(r"^[-*]\s+(.*)", stripped)
        if bulleted:
            acs.append(bulleted.group(1))
            continue
        if acs and stripped:
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
                           model: str | None = _MISSING,
                           model_source: str | None = _MISSING,
                           phase2_completed: bool = False) -> str:
    """Assemble the canonical issue-mode audit report.

    *ac_results* is a list of ``{"text": ..., "verdict": ..., "evidence": ...}``.
    *child_results* is a list of child review dicts with keys:
      ``title``, ``id``, ``status``, ``stage``, ``ac_results``.
    *code_quality_findings* is an optional list of finding dicts from
      code quality checks. Each dict has ``severity``, ``file``, ``line``,
      ``message``, ``linter``, ``code`` keys.
    *code_quality_skipped_reason* is an optional string explaining why
      code quality was not run (e.g., no linters available).
    *model* is the name of the model used for the audit (e.g.,
      ``"opencode-go/deepseek-v4-flash"``). When not provided, no model
      line is emitted (backward compatibility). When provided as ``None``
      or empty string, the fallback ``Model: manual (no provider)`` is used.
    *model_source* is the source of the model (``"local"`` or ``"remote"``).
      When provided alongside *model*, produces
      ``Model: <model> (provider: <source>)``.

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
    # Check that all active children are in in_review or done stage
    active_children = [c for c in child_results if c.get("stage") not in ("", None)]
    all_children_reviewed = all(
        c.get("stage") in ("in_review", "done")
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
    cq_findings = code_quality_findings or []
    has_blocking_cq = any(
        f.get("severity") in ("critical", "high")
        for f in cq_findings
    )

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
            "", "## Summary", "",
        ]
    else:
        lines = [
            f"Ready to close: {ready}",
            "",
            f"Audit report for work item {issue_id_label}",
            "", "## Summary", "",
        ]

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
            if phase2_completed:
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

    if code_quality_skipped_reason:
        lines.append(f"Code quality check skipped: {code_quality_skipped_reason}")
    elif not cq_findings and cq_fixes == 0:
        lines.append("No code quality issues found.")
    elif not cq_findings and cq_fixes > 0:
        lines.append(f"All issues auto-fixed by **{cq_fixes}** linter(s).")
        lines.append("No remaining issues.")
    else:
        has_critical_or_high = any(
            f.get("severity") in ("critical", "high") for f in cq_findings
        )
        if has_critical_or_high:
            lines.append(
                "**Critical and/or high severity findings detected — "
                "these block closure.**"
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

    lines.append("")
    return "\n".join(lines)


def _assemble_child_audit_report(child: dict, ac_results: list[dict],
                                 model: str | None = _MISSING,
                                 model_source: str | None = _MISSING) -> str:
    """Assemble an audit report for a single child work item.

    *child* is a dict with keys ``title``, ``id``, ``status``, ``stage``.
    *ac_results* is a list of ``{"text": ..., "verdict": ..., "evidence": ...}``.
    *model* is the name of the model used for the audit. When not provided,
      no model line is emitted. When ``None`` or empty, the fallback
      ``Model: manual (no provider)`` is used.
    *model_source* is the source of the model (``"local"`` or ``"remote"``).

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
) -> tuple[bool, str]:
    """Assemble and persist an audit report for a single child work item.

    *model* and *model_source* are passed through to
    ``_assemble_child_audit_report()`` for inclusion in the child report.
    *worklog_dir* is forwarded to ``persist_audit`` so the wl invocation
    targets the correct worklog store regardless of the caller's cwd.

    Returns (success, report_text).
    On failure the report text is still returned so callers can log it.
    """
    child = {
        "title": child_title,
        "id": child_id,
        "status": child_status,
        "stage": child_stage,
    }
    report = _assemble_child_audit_report(child, ac_results, model=model, model_source=model_source)

    rc = persist_audit(child_id, report, worklog_dir=worklog_dir)
    success = rc == 0
    return success, report


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
                           max_retries: int | None = None) -> dict:
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

    If *debug_log* is provided the entry reason will be "debug_log" and the
    provided path will be used. If *debug_log* is not provided but the pi
    result contains diagnostic fields (``raw_stdout``/``raw_stderr``), a
    default path from ``_default_debug_log_path`` will be used and the reason
    will be "parse_failure".
    """
    result = _call_pi(prompt, model=model, pi_bin=pi_bin, enable_tools=enable_tools, timeout=timeout, max_retries=max_retries)

    # Emit a per-call timing line to stderr (performance baseline). Includes
    # issue id, call context, and elapsed seconds so Phase 2 durations are
    # measurable and regressions are visible without a debug log.
    elapsed = result.get("elapsed_seconds")
    if elapsed is not None:
        print(
            f"Per-call timing: issue_id={issue_id} context={context} "
            f"elapsed_seconds={float(elapsed):.2f}",
            file=sys.stderr,
        )

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
                             worklog_dir: str | None = None) -> tuple[bool | None, str]:
    """Check a child's persisted audit verdict via wl audit-show.

    Returns a (verdict, reason) tuple:
        (True, "ready")      — Child audit says "Ready to close: Yes"
        (False, "not_ready") — Child audit says "Ready to close: No"
        (None, "no_audit")   — No audit data found (audit-show returned null/empty)
        (None, "stale")      — Audit exists but is stale (within freshness buffer)
        (None, "error")      — wl audit-show command failed

    Freshness is determined by comparing the audit's auditedAt timestamp against
    the child's updatedAt timestamp plus AUDIT_FRESHNESS_BUFFER_SECONDS.
    """
    from datetime import datetime, timedelta, timezone

    try:
        data = _run_wl(runner, ["wl", "audit-show", child_id, "--json"],
                       worklog_dir=worklog_dir)
    except RuntimeError:
        return None, "error"

    if not isinstance(data, dict) or data.get("success") is False:
        return None, "error"

    audit = data.get("audit")
    if not audit:
        return None, "no_audit"

    raw_output = audit.get("rawOutput")
    if not raw_output:
        return None, "no_audit"

    audited_at = audit.get("auditedAt")
    if not audited_at:
        return None, "no_audit"

    # Check freshness against the child's updatedAt
    try:
        wi_data = _run_wl(runner, ["wl", "show", child_id, "--json"],
                          worklog_dir=worklog_dir)
    except RuntimeError:
        # Can't check freshness; treat as fresh since we have an audit
        pass
    else:
        work_item = wi_data.get("workItem", {}) if isinstance(wi_data, dict) else {}
        updated_at = work_item.get("updatedAt")
        if updated_at:
            try:
                audit_time_str = str(audited_at).replace("Z", "+00:00")
                update_time_str = str(updated_at).replace("Z", "+00:00")
                audit_time = datetime.fromisoformat(audit_time_str)
                update_time = datetime.fromisoformat(update_time_str)
                if audit_time.tzinfo is None:
                    audit_time = audit_time.replace(tzinfo=timezone.utc)
                if update_time.tzinfo is None:
                    update_time = update_time.replace(tzinfo=timezone.utc)
                freshness_threshold = update_time + timedelta(seconds=AUDIT_FRESHNESS_BUFFER_SECONDS)
                if not (audit_time > freshness_threshold):
                    return None, "stale"
            except (ValueError, TypeError):
                pass  # Can't parse timestamps; treat as fresh since we have an audit

    # Parse the raw output for "Ready to close:"
    for line in raw_output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Ready to close:"):
            verdict = stripped.split(":", 1)[1].strip()
            if verdict.lower() == "yes":
                return True, "ready"
            return False, "not_ready"

    return None, "no_audit"


def _has_phase1_blocking_issues(cq_findings: list[dict], child_results: list[dict]) -> tuple[bool, str]:
    """Check whether Phase 1 automated screening has blocking issues.

    Returns (blocked, reason). If blocked, Phase 2 deep analysis should be
    skipped and all 'met' verdicts demoted to 'partial'.

    Blocking issues include:
    - Critical/high code quality findings
    - Children not in in_review/done stage
    - Active children in pre-review stages whose persisted audit says
      "Ready to close: No" (children in ``in_review`` stage are exempt
      from child audit verdict checking — per the audit spec, only
      pre-review stages block)
    """
    # Check code quality findings
    for f in cq_findings:
        if f.get("severity") in ("critical", "high"):
            return True, f"Critical/high code quality finding: {f.get('file', '?')}:{f.get('line', 0)} — {f.get('message', '')}"

    # Check children stages — skip deleted children
    active_children = [
        c for c in child_results
        if c.get("stage") not in ("", None) and c.get("status") != "deleted"
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
                      phase2_completed: bool = False) -> dict:
    """Build structured JSON payload for issue-mode audit.

    Ready-to-close logic:
      - All acceptance criteria (parent + children) must be ``met`` or ``adjusted``.
        ``adjusted`` criteria represent acceptable variance and do not block closure.
      - Critical/high code quality findings block closure.
      - Children in ``in_review`` or ``done`` stage are exempt from child
        audit verdict checks. Per the audit spec, children in ``in_review``
        do NOT block closure — only pre-review stages (``idea``,
        ``intake_complete``, ``plan_complete``) block.
    """
    all_ac_acceptable = all(
        r["verdict"] in _ACCEPTABLE_VERDICTS
        for r in ac_results + [c for cr in child_results for c in cr.get("ac_results", [])]
    )
    # Check that all active children are in in_review or done stage
    active_children = [c for c in child_results if c.get("stage") not in ("", None)]
    all_children_reviewed = all(
        c.get("stage") in ("in_review", "done")
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
    has_blocking_cq = any(
        f.get("severity") in ("critical", "high")
        for f in cq_findings
    )

    ready = all_ac_acceptable and all_children_reviewed and not has_blocking_cq and not any_child_audit_not_ready

    all_criteria = ac_results + [c for cr in child_results for c in cr.get("ac_results", [])]
    unmet_count = sum(1 for r in all_criteria if r["verdict"] == VERDICT_UNMET)
    adjusted_count = sum(1 for r in all_criteria if r["verdict"] == VERDICT_ADJUSTED)

    phase2_note = " Deep analysis completed." if phase2_completed else " Phase 2 skipped."
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
) -> tuple[int, dict, bool]:
    """Run Phase 2 deep analysis for a single child (worker for parallelism).

    Returns ``(ci, updated_child, timeout_occurred)``. Never raises on Pi
    failures: a ``RuntimeError`` from ``_call_pi_and_maybe_log`` falls back to
    the child's existing ``ac_results`` unchanged. A timeout marks the child's
    ACs ``partial`` and reports ``timeout_occurred=True`` (so the caller can
    set ``phase2_completed=False``).
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
        "SCANNING — When you need to look something up, use the bounded helpers:\n"
        "- Worklog lookups: `python3 skill/audit/scripts/scan.py find-workitem <id>` (never `grep -r` over .worklog/).\n"
        "- Code search: `python3 skill/audit/scripts/scan.py search-code <pattern> --path <dir> --type py` (bounded rg with prunes).\n"
        "- NEVER run unbounded recursive grep over the repo root or .worklog/ (e.g. `grep -r ... .` or `grep -r ... .worklog/`).\n"
        "- Single-file greps of `.worklog/worklog-data.jsonl` are permitted (e.g. `grep -n <id> .worklog/worklog-data.jsonl`).\n\n"
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
        )
    except RuntimeError:
        return ci, child, False

    # Handle child timeout: mark all child ACs as partial
    if child_result.get("_timeout"):
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
            deep_verdict = item.get("verdict", "")
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
) -> tuple[list[dict], list[dict], bool]:
    """Run Phase 2 deep code analysis.

    Calls Pi with a detailed prompt asking the model to read the actual
    implementation files and verify each acceptance criterion against
    what the code actually does. The prompt includes a file-scope manifest
    (Key Files, git changed files, repo index, Phase 1 evidence refs) so
    the model verifies in-scope files instead of exploring the whole repo.

    *runner* is used for git queries when building the file-scope manifest
    and defaults to ``_default_runner``.

    Returns (updated_ac_results, updated_child_results, phase2_completed).
    The ``phase2_completed`` flag is ``False`` when the Pi call times out,
    allowing the caller to set appropriate diagnostic evidence.
    """
    if runner is None:
        runner = _default_runner

    file_scope = _build_file_scope_manifest(issue, ac_results, runner=runner)

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
        "SCANNING — When you need to look something up, use the bounded helpers:\n"
        "- Worklog lookups: `python3 skill/audit/scripts/scan.py find-workitem <id>` (never `grep -r` over .worklog/).\n"
        "- Code search: `python3 skill/audit/scripts/scan.py search-code <pattern> --path <dir> --type py` (bounded rg with prunes).\n"
        "- File listing: `python3 skill/audit/scripts/scan.py list-files --path <dir> --type py`.\n"
        "- NEVER run unbounded recursive grep over the repo root or .worklog/ (e.g. `grep -r ... .` or `grep -r ... .worklog/`).\n"
        "- Single-file greps of `.worklog/worklog-data.jsonl` are permitted (e.g. `grep -n <id> .worklog/worklog-data.jsonl`).\n\n"
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
        )
    except RuntimeError as exc:
        # Phase 2 failure is non-fatal; log and fall back to Phase 1 results
        print(f"Warning: Phase 2 deep analysis failed: {exc}", file=sys.stderr)
        if script_failure_callback:
            script_failure_callback("pi (Phase 2 deep analysis)", exc)
        return ac_results, child_results, False

    # Check for timeout before attempting to parse results
    if result.get("_timeout"):
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
            deep_verdict = item.get("verdict", "")
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

    # Also run deep analysis on active children
    updated_children = list(child_results)
    child_timeout_occurred = False

    # Collect the (index, child) pairs that need parent deep analysis.
    # - Completed/done children are skipped (already closed).
    # - child_audit_ready=True children are skipped (P2 reuse — their own
    #   fresh audit already verified the code).
    # - Children without ac_results are skipped (nothing to verify).
    pending: list[tuple[int, dict]] = []
    for ci, child in enumerate(updated_children):
        if child.get("status") == "completed" and child.get("stage") == "done":
            continue
        if child.get("child_audit_ready") is True:
            continue
        if not child.get("ac_results"):
            continue
        pending.append((ci, child))

    parallelism = _resolve_phase2_parallelism()

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
            ))

    return updated_ac, updated_children, not child_timeout_occurred


def cmd_issue(issue_id: str, persist: bool = True,
              timeout: int | None = None,
              parent_timeout: int | None = None,
              pi_bin: str = "pi", model: str | None = None,
              model_source: str = DEFAULT_MODEL_SOURCE,
              runner: Runner | None = None, json_mode: bool = False,
              debug_log: str | None = None,
              force: bool = False,
              worklog_dir: str | None = None) -> int:
    """Audit a single work item.

    The resolved model name and source are included as a metadata line
    in the audit report output (issue-level and child reports).

    Model resolution order (highest first):
      1. --model CLI flag (explicit override)
      2. Config-driven: model.audit from .ralph.json resolved via model_source
      3. Hardcoded fallback: DEFAULT_MODEL

    When *force* is ``True``, the freshness gate is bypassed and a full
    audit pipeline is always run, even if a recent audit already exists.

    *worklog_dir* is an explicit ``--worklog-dir`` value that overrides
    auto-resolution for every wl call made by this run (see
    ``_resolve_worklog_flags``).

    For each active child (not completed/done), the child's persisted audit
    verdict is checked via ``wl audit-show``. If no audit exists or the audit
    is stale, an audit is auto-triggered for that child (via the same audit
    runner mechanism) and the resulting verdict is evaluated. A child whose
    audit says "Ready to close: No" prevents the parent from being ready to
    close. This check is performed before Phase 1 screening so that Phase 1
    can block on children not individually ready.
    """
    # Resolve the effective model from config + CLI
    config = _load_config()
    resolved_model = _resolve_model_for_phase(
        AUDIT_PHASE, config, model_source, cli_model=model,
    )

    if runner is None:
        runner = _default_runner

    # ------------------------------------------------------------------
    # Freshness gate: skip if a recent audit already exists
    # (before status lifecycle to avoid unnecessary in_progress transitions)
    # ------------------------------------------------------------------
    if not force:
        fresh_report = _check_audit_freshness(runner, issue_id,
                                              worklog_dir=worklog_dir)
        if fresh_report is not None:
            print("Skipping: audit still fresh")
            print(fresh_report)
            return 0

    # Track script execution failures for prominent surfacing
    script_failure: dict | None = None

    def _record_script_failure(script_name: str, exc: Exception) -> None:
        """Record a script execution failure into the enclosing scope.

        Only records the first failure; subsequent failures are suppressed
        to avoid overwriting the root cause.
        """
        nonlocal script_failure
        if script_failure is not None:
            return
        reason = str(exc)
        if isinstance(exc, subprocess.TimeoutExpired):
            reason = f"Timeout after {exc.timeout}s"
        elif isinstance(exc, FileNotFoundError):
            reason = f"File not found: {exc.filename}"
        script_failure = {
            "script_name": script_name,
            "reason": reason,
            "stderr": str(exc),
        }

    # ------------------------------------------------------------------
    # Capture original status + stage before setting in_progress, so the
    # finally block can restore a safe consistent state on failure and
    # apply a verdict-driven terminal transition on success.
    # ------------------------------------------------------------------
    original_status = "open"  # safe default
    original_stage = ""       # safe default (unknown)
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

    # Audit verdict parsed from the assembled report ("yes"/"no"); stays
    # None when the audit failed or the verdict could not be parsed.
    audit_verdict: str | None = None
    # Set True only when the audit pipeline reached a successful completion
    # (report assembled and, when persisting, persisted + read back).
    audit_completed: bool = False

    # ------------------------------------------------------------------
    # Status lifecycle: set in_progress on entry (verdict-driven on exit)
    # ------------------------------------------------------------------
    _run_wl(runner, ["wl", "update", issue_id, "--status", "in_progress", "--json"],
            worklog_dir=worklog_dir)

    try:
        try:
            data = _run_wl(runner, ["wl", "show", issue_id, "--children", "--json"],
                           worklog_dir=worklog_dir)
        except RuntimeError as exc:
            _record_script_failure("wl show", exc)
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

        # ------------------------------------------------------------------
        # Code quality check (before AC verification)
        # ------------------------------------------------------------------
        cq_findings: list[dict] = []
        cq_fixes_applied: int = 0
        cq_skipped_reason: str | None = None
        try:
            from skill.code_review.scripts.code_quality import run_code_quality
            cq_result = run_code_quality(project_root=TARGET_PROJECT_ROOT, runner=runner, fix=True)
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

        # Track elapsed time so we can skip remaining child audits if we
        # approach the parent bash-tool timeout. This ensures a graceful
        # degradation instead of a silent external kill. The threshold is
        # configurable via --parent-timeout / AUDIT_PARENT_TIMEOUT
        # (default PARENT_TIMEOUT_DEFAULT).
        elapsed_guard = (
            parent_timeout if parent_timeout is not None else PARENT_TIMEOUT_DEFAULT
        )
        _audit_start = time.monotonic()

        def _elapsed():
            return time.monotonic() - _audit_start

        # Review parent ACs via Pi (batched into a single call for performance)
        ac_results = []
        if acs and acs[0] != "No acceptance criteria defined.":
            ac_list_json = json.dumps([{"index": i, "text": ac} for i, ac in enumerate(acs)])
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
                f"Criteria: {ac_list_json}"
            )
            try:
                result = _call_pi_and_maybe_log(issue_id, "parent", prompt, model=resolved_model, pi_bin=pi_bin, debug_log=debug_log, timeout=timeout)
            except RuntimeError as exc:
                _record_script_failure("pi (parent AC review)", exc)
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
                        "verdict": item.get("verdict", "unmet"),
                        "evidence": item.get("evidence", ""),
                    })
            else:
                # Fallback: this path is reached when the Pi response was not a
                # parseable JSON array. Print a warning, log raw output, and use
                # 'partial' verdict with diagnostic evidence instead of silently
                # falling back to 'unmet' with empty evidence.
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

        # Review children (depth 1 only, ignore deleted)
        # Children with status=deleted (wl delete) or deletedBy (imported)
        # are excluded from active children so they don't block parent closure.
        # Pass ALL active children to the assembler; it handles the cap.
        # Note: children with status=completed/stage=done are included for
        # reporting but exempted from blocking checks by _has_phase1_blocking_issues.
        child_results = []
        active_children = [
            c for c in children
            if c.get("status") != "deleted" and not c.get("deletedBy")
        ]
        for child in active_children:
            # Skip remaining children if we're too close to the parent
            # timeout (elapsed_guard). This prevents a silent external kill
            # and instead produces a clear diagnostic for skipped audits.
            if _elapsed() >= elapsed_guard:
                print(
                    f"Warning: Approaching parent timeout ({_elapsed():.0f}s elapsed). "
                    f"Skipping child {child.get('id', '')} ({child.get('title', '')}). "
                    "Manual audit required for this child.",
                    file=sys.stderr,
                )
                child_results.append({
                    "title": child.get("title", ""),
                    "id": child.get("id", ""),
                    "status": child.get("status", ""),
                    "stage": child.get("stage", ""),
                    "ac_results": [{
                        "text": "Skipped due to audit timeout. Manual audit required.",
                        "verdict": "unmet",
                        "evidence": (
                            f"Audit runner skipped this child after "
                            f"{_elapsed():.0f}s total elapsed time to avoid "
                            f"the parent process timeout ({elapsed_guard}s "
                            f"budget). Manual audit required."
                        ),
                    }],
                })
                continue

            child_desc = child.get("description", "")
            child_acs = _extract_acs(child_desc)
            child_ac_results = []
            if child_acs and child_acs[0] != "No acceptance criteria defined.":
                # Batch child ACs into a single pi call
                child_ac_list = json.dumps([{"index": i, "text": ac} for i, ac in enumerate(child_acs)])
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
                    f"If a criterion has acceptable variance (implementation differs from original "
                    f"spec but still satisfies user story intent), use verdict 'adjusted' instead of 'unmet'. "
                    f"Include justification in the evidence field.\n\n"
                    f"Criteria: {child_ac_list}"
                )
                try:
                    result = _call_pi_and_maybe_log(issue_id, f"child:{child.get('id', '')}", prompt, model=resolved_model, pi_bin=pi_bin, debug_log=debug_log, timeout=timeout)
                except RuntimeError as exc:
                    _record_script_failure("pi (child AC review)", exc)
                    print(f"Warning: Pi call failed for child AC review: {exc}", file=sys.stderr)
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
                            "verdict": item.get("verdict", "unmet"),
                            "evidence": item.get("evidence", ""),
                        })
                else:
                    # Fallback: this path is reached when the Pi response was not a
                    # parseable JSON array. Print a warning, log raw output, and use
                    # 'partial' verdict with diagnostic evidence instead of silently
                    # falling back to 'unmet' with empty evidence.
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
                            target = Path(debug_log) if debug_log else _default_debug_log_path(issue_id, f"child_{child.get('id', 'unknown')}_ac_fallback")
                            _write_debug_log(target, {
                                "issue_id": issue_id,
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
            child_results.append({
                "title": child.get("title", ""),
                "id": child.get("id", ""),
                "status": child.get("status", ""),
                "stage": child.get("stage", ""),
                "ac_results": child_ac_results,
            })

        # ------------------------------------------------------------------
        # Check each active child's persisted audit verdict.
        # For children without audits or with stale audits, auto-trigger
        # a fresh audit (if persist is True) and re-evaluate.
        # Children with completed/done status+stage are exempt (AC5).
        # ------------------------------------------------------------------
        _audit_runner_path = Path(__file__).resolve()

        for child in child_results:
            # Skip completed/done children (exempt per AC5)
            if child.get("status") == "completed" and child.get("stage") == "done":
                child["child_audit_ready"] = True  # Exempt - treat as ready
                continue

            verdict, reason = _get_child_audit_verdict(runner, child["id"],
                                                       worklog_dir=worklog_dir)

            if verdict is None and persist:
                if _elapsed() < elapsed_guard:
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
                        verdict, reason = _get_child_audit_verdict(runner, child["id"],
                                                                   worklog_dir=worklog_dir)
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
                        f"({child['title']}). Manual audit required.",
                        file=sys.stderr,
                    )

            # Set child_audit_ready: True/False if verdict is known, False otherwise
            child["child_audit_ready"] = verdict if verdict is not None else False

        # Initialize child_persist_results for reporting
        child_persist_results = []

        # Persist child audits to individual child work items (if persist is True)
        if persist:
            for child in child_results:
                child_success, _child_report = _persist_child_audit(
                    child_id=child["id"],
                    child_title=child["title"],
                    child_status=child["status"],
                    child_stage=child["stage"],
                    ac_results=child["ac_results"],
                    pi_bin=pi_bin,
                    model=resolved_model,
                    model_source=model_source,
                    worklog_dir=worklog_dir,
                )
                child_persist_results.append({
                    "id": child["id"],
                    "title": child["title"],
                    "success": child_success,
                })
                if not child_success:
                    print(
                        f"Warning: Failed to persist audit for child {child['id']} "
                        f"({child['title']}): wl returned exit code {1}",
                        file=sys.stderr,
                    )

        # ------------------------------------------------------------------
        # Phase 2 gate: check if Phase 1 automated screening has blocking issues
        # ------------------------------------------------------------------
        phase1_blocked, phase1_reason = _has_phase1_blocking_issues(
            cq_findings, child_results
        )
        phase2_completed = False

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
        else:
            # Phase 1 passed → run Phase 2 deep code analysis
            print("Phase 1 passed: running Phase 2 deep code analysis...", file=sys.stderr)
            ac_results, child_results, phase2_completed = _run_phase2_deep_analysis(
                work_item, ac_results, child_results,
                resolved_model=resolved_model,
                pi_bin=pi_bin,
                debug_log=debug_log,
                script_failure_callback=_record_script_failure,
                timeout=timeout,
                runner=runner,
            )

        # ------------------------------------------------------------------
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

        # Assemble and output report
        report = _assemble_issue_report(
            work_item, ac_results, child_results,
            code_quality_findings=cq_findings,
            code_quality_fixes_applied=cq_fixes_applied,
            code_quality_skipped_reason=cq_skipped_reason,
            model=resolved_model,
            model_source=model_source,
            phase2_completed=phase2_completed,
        )

        # Wrap report with failure notice if any subprocess calls failed
        if script_failure:
            notice = FailureNotice(
                script_name=script_failure["script_name"],
                reason=script_failure["reason"],
                stderr_context=script_failure["stderr"],
            )
            report = notice.wrap(report)

        # Capture the audit verdict for the status lifecycle transition.
        # The finally block only trusts this verdict when the audit pipeline
        # completed successfully (no script failures).
        audit_verdict = _parse_ready_to_close(report)

        if json_mode:
            payload = _build_issue_json(
                work_item, ac_results, child_results,
                code_quality_findings=cq_findings,
                code_quality_fixes_applied=cq_fixes_applied,
                phase2_completed=phase2_completed,
            )
            payload["child_persist_results"] = child_persist_results
            # Include script failure info in JSON output
            if script_failure:
                payload["script_failure"] = {
                    "script_name": script_failure["script_name"],
                    "reason": script_failure["reason"],
                    "stderr": script_failure.get("stderr", ""),
                }
            print(json.dumps(payload, indent=2))
        else:
            print(report, end="")
            # Print closing sentence (stdout UX – not persisted)
            print()
            print(_get_closing_sentence(report))

        if persist:
            persist_rc = persist_audit(issue_id, report, worklog_dir=worklog_dir)
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
        # ------------------------------------------------------------------
        # Status lifecycle: verdict-driven terminal transition on exit.
        # Always runs because of try/finally — the item is never left
        # in_progress after the audit completes.
        #
        #   Ready to close: Yes → completed / in_review (stage kept 'done')
        #   Ready to close: No  → open / plan_complete
        #   Failure / timeout / unparseable verdict → safe consistent state
        #       (captured original status+stage, forced open/plan_complete
        #       when the original was in_progress) + assignee cleared so the
        #       item returns fully to the actionable queue.
        # ------------------------------------------------------------------
        try:
            if script_failure is not None or not audit_completed or audit_verdict is None:
                # Failure / unparseable verdict: never leave in_progress.
                was_in_progress = original_status in ("in_progress", "in-progress")
                safe_status = "open" if was_in_progress else original_status
                safe_stage = "plan_complete" if was_in_progress else original_stage
                if not safe_stage:
                    # Stage unknown (capture failed): pick a stage valid for
                    # the restored status so wl never rejects the combo.
                    safe_stage = "in_review" if safe_status == "completed" else "plan_complete"
                _run_wl(runner, [
                    "wl", "update", issue_id,
                    "--status", safe_status,
                    "--stage", safe_stage,
                    "--assignee", "",
                    "--json",
                ], worklog_dir=worklog_dir)
            elif audit_verdict == "yes":
                # Advance to the review queue. Keep a terminal 'done' stage.
                if original_stage == "done":
                    _run_wl(runner, ["wl", "update", issue_id, "--status", "completed", "--json"],
                            worklog_dir=worklog_dir)
                else:
                    _run_wl(runner, ["wl", "update", issue_id, "--status", "completed", "--stage", "in_review", "--json"],
                            worklog_dir=worklog_dir)
            else:  # audit_verdict == "no"
                # Return to the actionable queue at a fixed pre-review stage.
                _run_wl(runner, ["wl", "update", issue_id, "--status", "open", "--stage", "plan_complete", "--json"],
                        worklog_dir=worklog_dir)
        except RuntimeError:
            pass  # Status update failure must not mask the main result


# ---------------------------------------------------------------------------
# Subcommand: project
# ---------------------------------------------------------------------------

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
                worklog_dir: str | None = None) -> int:
    """Audit the overall project.

    Model resolution order (highest first):
      1. --model CLI flag (explicit override)
      2. Config-driven: model.audit from .ralph.json resolved via model_source
      3. Hardcoded fallback: DEFAULT_MODEL

    *worklog_dir* is an explicit ``--worklog-dir`` value that overrides
    auto-resolution for every wl call made by this run (see
    ``_resolve_worklog_flags``).
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
        nonlocal script_failure
        if script_failure is not None:
            return
        reason = str(exc)
        if isinstance(exc, subprocess.TimeoutExpired):
            reason = f"Timeout after {exc.timeout}s"
        elif isinstance(exc, FileNotFoundError):
            reason = f"File not found: {exc.filename}"
        script_failure = {
            "script_name": script_name,
            "reason": reason,
            "stderr": str(exc),
        }

    try:
        data = _run_wl(runner, ["wl", "list", "--json"],
                       worklog_dir=worklog_dir)
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
    work_items = data.get("workItems", data) if isinstance(data, dict) else data
    in_progress = [w for w in work_items if w.get("status") == "in_progress"] if isinstance(work_items, list) else []
    blocked = [w for w in work_items if w.get("status") == "blocked"] if isinstance(work_items, list) else []
    completed = [w for w in work_items if w.get("status") == "completed"] if isinstance(work_items, list) else []

    summary = (
        f"Project-level audit: {len(in_progress)} items in progress, "
        f"{len(blocked)} blocked, {len(completed)} completed."
    )

    if blocked:
        blocked_ids = ", ".join(w.get("id", "?") for w in blocked[:5])
        recommendation = (
            f"Review blocked items {blocked_ids} to unblock progress."
        )
    else:
        recommendation = "No specific recommendations at this time."

    # Optional: call Pi for project-level summary
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
        if pi_result.get("verdict") == "met" and pi_result.get("evidence"):
            # Use Pi's response if parseable
            pass  # Could enhance this in future
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
    p_issue.add_argument("--parent-timeout", type=int, default=None,
                         help=(
                             "Override the cumulative elapsed-time guard in seconds "
                             "(default: " + str(PARENT_TIMEOUT_DEFAULT) + "); raise this to "
                             "audit children that would otherwise be skipped "
                             "(env: AUDIT_PARENT_TIMEOUT)"
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
    p_issue.add_argument("--worklog-dir", default=None,
                         help="Explicit .worklog directory to target (overrides auto-resolution)")
    p_issue.add_argument("--max-concurrency", type=int, default=None,
                         help="Max concurrent pi/audit subprocesses (default: AUDIT_MAX_CONCURRENCY env or 2)")

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

    if args.command == "issue":
        return cmd_issue(args.issue_id, persist=not args.do_not_persist,
                         timeout=_resolve_effective_timeout(args.timeout),
                         parent_timeout=_resolve_parent_timeout(args.parent_timeout),
                         pi_bin=args.pi_bin, model=args.model,
                         model_source=args.model_source, json_mode=args.json,
                         debug_log=args.debug_log,
                         force=args.force,
                         worklog_dir=args.worklog_dir)
    elif args.command == "project":
        return cmd_project(timeout=_resolve_effective_timeout(args.timeout),
                           pi_bin=args.pi_bin, model=args.model,
                           model_source=args.model_source, json_mode=args.json,
                           debug_log=args.debug_log,
                           worklog_dir=args.worklog_dir)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
