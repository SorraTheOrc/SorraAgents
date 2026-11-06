#!/usr/bin/env python3

# <!-- REFACTOR-SA-0MVN0IJ7H008OORA
# smell: unknown
# severity: low
# description: Unparenthesized implicit string concatenation in collection
# -->

# <!-- REFACTOR-SA-0MUQ2OU12009Q8V5
# smell: formatting
# severity: low
# description: Shebang is present but file is not executable
# -->

# <!-- REFACTOR-SA-0MUMEJX7G009PKO5
# smell: ruff_specific
# severity: medium
# description: Unpacked variable `stdout` is never used
# -->
"""Orchestration script for the implement skill workflow.

Manages the deterministic lifecycle of an implementation work item: claim,
worktree creation, signal handling, build/test/commit cycle, cleanup, and
stage advancement.

Usage:
  implement.py start <work-item-id>          # Phase 1: setup
  implement.py finish <work-item-id>         # Phase 2: build, test, commit, push, cleanup
  implement.py abort <work-item-id>          # Abort and cleanup
  implement.py cleanup <work-item-id>        # Remove a finished item's worktree (run LAST)
  implement.py parent <parent-id>            # Recurse into children (epic/parent items)
  implement.py drive <parent-id>             # Drive ALL children to completion, one fresh session each

Optional flags:
  --json                    JSON output for agents
  --no-refactor             Skip the refactor step
  --max-retry N             Max test-fix loop retries (default: 3)
  --commit-msg <msg>        Commit message override
  --parent-branch <branch>  Override parent branch (default: dev)
  --worktree-path <path>    Override worktree path
  --allow-orphaned-stashes   Acknowledge orphaned-stash warning and proceed
  --max-child-sessions N    Max fresh sessions per child when driving (default: 1)
  --finish-attempt-limit N  Max failing finish executions per child when driving
                            (default: the --max-child-sessions value)
  --infra-retry-limit N     Max infrastructure-refusal retries per child when
                            driving (default: 5; a separate budget, never
                            consumes --finish-attempt-limit, spawns no session)
  --child-timeout N         Per-child session timeout in seconds (default: 3600)
  --pane-sessions / --no-pane-sessions
                            Drive children in interactive Herdr panes when Herdr
                            is detected (default: env-driven via
                            IMPLEMENT_DRIVE_PANE_SESSIONS)
  --focus / --no-focus      Focus each new child pane (default: --no-focus)
  --finalise-stranded       Finalise evidence-gated stranded children when driving
  -v, --verbose             Verbose logging

Environment:
  IMPLEMENT_TEST_COMMAND    Override the finish test-step command (shell string)
  IMPLEMENT_DRIVE_PI_BIN    Pi executable used to spawn child sessions (default: pi)
  IMPLEMENT_DRIVE_CHILD_TIMEOUT  Per-child session timeout in seconds
  IMPLEMENT_DRIVE_PANE_SESSIONS  Opt in to the pane-driven transport (same as
                            --pane-sessions); needs HERDR_ENV=1 + herdr binary

Exit codes:
  0 – success
  1 – error during execution (non-abort)
  2 – aborted
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import functools
import json
import logging
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback

#: Bound at import time so a test that patches ``implement.subprocess`` with
#: a Mock (replacing the whole module) cannot break `except` clauses that
#: reference it — a MagicMock attribute is not an exception class and would
#: raise ``TypeError: catching classes that do not inherit from
#: BaseException`` (SA-0MVVF75ZA007XNKB).
_SUBPROCESS_TIMEOUT_EXPIRED = subprocess.TimeoutExpired

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure the repository root is on sys.path so skill package imports work
_SKILLS_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_ROOT_STR = str(_SKILLS_ROOT)
if _SKILLS_ROOT_STR in sys.path:
    sys.path.remove(_SKILLS_ROOT_STR)
sys.path.insert(0, _SKILLS_ROOT_STR)

from import_guard import guard_shared_import

try:
    from shared.code_freeze import is_code_freeze_active
    from shared.herdr_pane import resolve_herdr_bin as _resolve_herdr_bin_path
    from shared.herdr_pane import update_pane_title as _update_pane_title
    from shared.herdr_session import HerdrSessionClient, agent_name_for_child
    from shared.repo_lock import DEFAULT_LOCK_RELPATH as _REPO_LOCK_RELPATH
    from shared.repo_lock import RepoLock, release_repo_lock
    from shared.skill_extensions import (
        SkillExtension,
        SkillExtensionError,
        load_extension,
    )
    from shared.status_lifecycle import (
        StatusLifecycle,
        resolve_worklog_dir,
        resolve_worklog_flags,
    )
    from shared.timing import Timer
    from shared.work_item_lock import (
        ENV_LOCK_TOKEN,
        WorkItemLock,
        lock_file_path,
    )
except ModuleNotFoundError as _missing_shared:
    guard_shared_import(_missing_shared.name)

try:
    # Fail-open self-referential-upstream warning (SA-0MVX35AUC003CNUD AC2/
    # AC3). Deliberately OUTSIDE the hard-fail shared block above: the
    # detection module is optional, so a partial skill install or a repo
    # without it degrades to a logged skip instead of blocking the start.
    from shared.self_referential_upstream import (
        detect_self_referential_upstreams as _detect_self_referential_upstreams,
    )
except Exception:  # noqa: BLE001 -- broad by design; see the comment below
    # Broad by design (BLE001): a missing module, a missing runtime dependency
    # or a module-level guard clause must ALL degrade to the logged skip below
    # and never block a start. Pinned by
    # skill/implement/tests/test_optional_import_fail_open.py.
    _detect_self_referential_upstreams = None  # pragma: no cover - partial install

try:
    # Disk-backed TMPDIR resolution shared with the test runner (F1,
    # SA-0MW3EN72O005BMIP). Deliberately OUTSIDE the hard-fail shared block:
    # an older skill install without the module degrades to the ambient
    # TMPDIR — logged and unchanged — instead of blocking the start
    # (SA-0MW3ENQY0001RBSW AC4, fail-open).
    from shared.test_tmpdir import resolve_test_tmpdir as _resolve_test_tmpdir
except Exception:  # noqa: BLE001 -- broad by design; see the comment below
    # Broad by design (BLE001): a partial skill install without the
    # test_tmpdir module must degrade to the ambient TMPDIR — logged and
    # unchanged — and must never block a start (SA-0MW3ENQY0001RBSW AC4,
    # fail-open). Mirrors the self_referential_upstream guard above.
    _resolve_test_tmpdir = None  # pragma: no cover - partial install

try:
    # The shared changed-file → test selector (SA-0MT6CENPW004V2JN). Kept
    # optional: partial skill installs (staged layouts without the test
    # skill's scripts) must still start implement.py — changed-scope then
    # degrades to full scope with a warning.
    from test.scripts.run_tests import (
        TestConcurrencyTimeout as _TestConcurrencyTimeout,
    )
    from test.scripts.run_tests import (
        changed_scope_commands as _changed_scope_commands,
    )
    from test.scripts.run_tests import (
        changed_scope_selection as _changed_scope_selection,
    )
    from test.scripts.run_tests import (
        extension_suite_commands as _extension_suite_commands,
    )
    from test.scripts.run_tests import (
        full_suite_commands as _full_suite_commands,
    )
    from test.scripts.run_tests import (
        guarded_run_all as _guarded_run_all,
    )
    from test.scripts.run_tests import paced_runner as _test_paced_runner
except ModuleNotFoundError:
    _changed_scope_commands = None  # type: ignore[assignment]
    _changed_scope_selection = None  # type: ignore[assignment]
    _extension_suite_commands = None  # type: ignore[assignment]
    _full_suite_commands = None  # type: ignore[assignment]
    _guarded_run_all = None  # type: ignore[assignment]
    _test_paced_runner = None  # type: ignore[assignment]

    class _TestConcurrencyTimeout(RuntimeError):  # type: ignore[no-redef]
        """Fallback when the test skill is unavailable (never raised)."""

try:
    # Stable gate-failure notice contract (WL-0MVGUT787000LNDG). Imported
    # separately so an older test skill without the constants degrades to
    # the literal fallbacks instead of breaking the whole delegation import.
    from test.scripts.run_tests import (
        NOTICE_KILLED as _NOTICE_KILLED,
    )
    from test.scripts.run_tests import (
        NOTICE_SLOT_BUSY as _NOTICE_SLOT_BUSY,
    )
    from test.scripts.run_tests import (
        NOTICE_SUITE_TIMEOUT as _NOTICE_SUITE_TIMEOUT,
    )
    from test.scripts.run_tests import (
        NOTICE_UNPARSED_FAILURE as _NOTICE_UNPARSED_FAILURE,
    )
    from test.scripts.run_tests import (
        SLOT_BUSY_NOTICE_PREFIX as _SLOT_BUSY_NOTICE_PREFIX,
    )
except ImportError:
    # Older test skill without the notice-code contract — degrade to the
    # literal fallbacks (never break the delegation import chain).
    _NOTICE_SLOT_BUSY = "slot_busy"  # type: ignore[assignment]
    _NOTICE_SUITE_TIMEOUT = "suite_timeout"  # type: ignore[assignment]
    _NOTICE_KILLED = "killed"  # type: ignore[assignment]
    _NOTICE_UNPARSED_FAILURE = "unparsed_failure"  # type: ignore[assignment]
    _SLOT_BUSY_NOTICE_PREFIX = (  # type: ignore[assignment]
        "test concurrency slot busy:"
    )

try:
    # Single shared gate-failure classifier (SA-0MVSJX5HS002DJHI AC3): the
    # pre-push hook and this module must never fork the reason vocabulary.
    # Imported separately so an older test skill degrades to the local
    # fallback body in ``_gate_failure_kind`` instead of breaking the import.
    from test.scripts.gate_reason import (
        GATE_REMEDIATION as _GATE_REMEDIATION,
    )
    from test.scripts.gate_reason import (
        classify_gate_result as _classify_gate_result,
    )
    from test.scripts.gate_reason import (
        state_changed_detail as _state_changed_detail,
    )
except ImportError:  # pragma: no cover - older test skill
    _classify_gate_result = None  # type: ignore[assignment]
    _GATE_REMEDIATION = {}  # type: ignore[assignment]

from test_cache import query_cached, run_cached
from test_runner import canonicalize_quiet_test_command

# Canonical quiet full-suite commands — identical cache keys to the test
# skill's run_tests.py (SA-0MSN6FBFS006Z5QP) so cached runs are shared
# full-suite evidence rather than a fail-fast partial run.
PYTEST_CMD = canonicalize_quiet_test_command("pytest")  # pytest -q -r a --disable-warnings
NPM_TEST_CMD = canonicalize_quiet_test_command("npm test")  # npm --silent test

LOG = logging.getLogger("implement.scripts.implement")


#: Module-level cache for the disk-backed TMPDIR, resolved once per finish
#: run (SA-0MW3ENQY0001RBSW). ``None`` means "not yet resolved"; the
#: resolver never returns ``None`` once resolved (a failed resolution is
#: recorded as ``_FINISH_TMPDIR_RESOLVED = True`` with ``None`` so callers
#: leave the child env unchanged and no re-resolution/log spam occurs).
_FINISH_TMPDIR_CACHE: str | None = None
_FINISH_TMPDIR_RESOLVED = False


def _resolved_finish_tmpdir() -> str | None:
    """Return the disk-backed TMPDIR, resolved once per finish run.

    Delegates to the shared F1 helper
    (:func:`shared.test_tmpdir.resolve_test_tmpdir`, SA-0MW3EN72O005BMIP) so
    the finish gate's build/test/push children share the exact same
    disk-backed directory the test runner uses — closing the inode-exhausted
    tmpfs failure end-to-end (SA-0MW3ENQY0001RBSW).

    Fail-open (AC4): a resolver outage (partial skill install without the
    F1 module, or any exception) is logged and ``None`` is returned once, so
    the push/build proceed with the ambient environment and an unchanged
    exit status. Callers only add ``TMPDIR`` when this returns a value.

    Operator guidance (precedence, prune rules, failure signature, remedy):
    ``docs/dev/test-skill-reference.md`` §3 (SA-0MW3ENVLU0097JRD).

    Returns:
        The resolved TMPDIR string, or ``None`` when the resolver is
        unavailable or failed (use the ambient environment, unchanged).
    """
    global _FINISH_TMPDIR_CACHE, _FINISH_TMPDIR_RESOLVED
    if _FINISH_TMPDIR_RESOLVED:
        return _FINISH_TMPDIR_CACHE
    _FINISH_TMPDIR_RESOLVED = True
    if _resolve_test_tmpdir is None:
        LOG.warning(
            "Finish TMPDIR resolver unavailable (partial install); "
            "using the ambient environment",
        )
        return None
    try:
        path, reason = _resolve_test_tmpdir()
    except Exception as exc:  # noqa: BLE001 - documented fail-open contract
        LOG.warning(
            "Finish TMPDIR resolution failed (%s); using the ambient "
            "environment",
            exc,
        )
        return None
    _FINISH_TMPDIR_CACHE = str(path)
    if "disk-backed candidate" in reason or "explicit" in reason:
        LOG.info("Finish TMPDIR=%s (%s)", path, reason)
    else:
        LOG.warning(
            "Finish TMPDIR candidate unusable; using %s (%s)", path, reason
        )
    return _FINISH_TMPDIR_CACHE


@contextlib.contextmanager
def _scoped_test_tmpdir_export() -> Iterator[None]:
    """Temporarily export the resolved disk-backed TMPDIR into ``os.environ``.

    The in-process ``guarded_run_all`` test step has no Popen to pass an env
    to (SA-0MW3ENQY0001RBSW AC2) — exporting around the call lets the test
    runner's *own* process tempfile usage and any child it spawns without an
    explicit env land on the disk-backed dir. Save/restore guarantees the
    export never leaks into the operator's parent shell after the finish run
    returns (AC3): the pre-existing value (or absence) is restored on exit.

    Fail-open (AC4): when resolution failed the context is a transparent
    no-op — nothing is exported, nothing is mutated.
    """
    tmpdir = _resolved_finish_tmpdir()
    if tmpdir is None:
        yield
        return
    previous = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = tmpdir
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous


def _emit_timing(timer: Timer) -> None:
    """Emit the timing report for a completed implement run to stderr.

    Timing always goes to stderr so stdout stays clean for JSON-mode output
    (SA-0MT319YGQ002E801 AC5 — additive, non-breaking).
    """
    print(timer.render(), file=sys.stderr)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path.cwd().resolve()
DEFAULT_PARENT_BRANCH = "dev"
DEFAULT_WORKTREE_DIR = ".worklog/worktrees"
DEFAULT_MAX_RETRY = 3

#: Stable marker carried by the completion comment. A re-run of ``finish``
#: deduplicates on it (plus the commit hash) so an interrupted run converges
#: without posting the same completion record twice
#: (SA-0MVXUSJJY009HMQ0 AC4).
COMPLETION_COMMENT_MARKER = "Implementation complete."

#: Stable marker carried by an interruption comment. Every interruption path
#: records the retained state once per commit and deduplicates on this marker
#: (plus the commit hash), so a recovered interruption is never re-recorded
#: (SA-0MVXUSJJY009HMQ0 AC2/AC4).
INTERRUPTION_COMMENT_MARKER = "Run interrupted"

#: Max fresh sessions spawned per child by ``phase_drive`` before it stops.
DEFAULT_MAX_CHILD_SESSIONS = 1
#: Max infrastructure-refusal retries per child in one ``phase_drive``
#: invocation. A transient infrastructure refusal (``local_dev_diverged``,
#: a held main-checkout lock, a dropped push transport) is retried on this
#: **separate** bounded budget with exponential backoff — it never consumes
#: the finish-attempt bound (``--finish-attempt-limit``) and never spawns a
#: session (LP-0MVWEP292000ODBS).
DEFAULT_INFRA_RETRY_LIMIT = 5
#: Default per-child session timeout (seconds) used by ``phase_drive``.
#: A child runs the whole implement workflow (changed-scope tests, full-suite
#: gate, and the pre-push hook) so this is much larger than the per-command
#: test timeout.
DEFAULT_CHILD_SESSION_TIMEOUT = 3600
#: Environment marker set on driven child sessions. When present,
#: ``phase_drive`` refuses to run — a driven session implements its own work
#: item with the leaf workflow and must never re-enter the driver. It also
#: marks "a driven child session owns this worktree", so ``finish`` defers
#: worktree removal to the driver instead of deleting the session's cwd
#: (SA-0MVTE8XAJ002IUDR).
DRIVE_RECURSION_ENV = "IMPLEMENT_DRIVE_ACTIVE"
#: Explicit opt-in that defers worktree removal in ``finish`` (and in the
#: signal handler) for **any** session, not just a driven child — the escape
#: hatch an operator (or a future pane transport) uses when the finishing
#: session still needs its cwd. Same ``"1"`` convention as
#: :data:`DRIVE_RECURSION_ENV`.
KEEP_WORKTREE_ENV = "IMPLEMENT_KEEP_WORKTREE"
#: Absolute path to the driven child's worktree, injected into its environment
#: so the child can verify its worktree root before its first edit — ``cwd``
#: alone does not confine an agent (it can ``cd`` to the main checkout).
WORKTREE_ENV = "IMPLEMENT_WORKTREE_PATH"
#: Explicit opt-in for the Herdr pane-driven transport (``--pane-sessions`` is
#: the CLI equivalent). Same ``"1"``-style convention as
#: :data:`DRIVE_RECURSION_ENV`. Absent/off keeps the headless ``pi -p`` path,
#: which automated callers (downtime worker, audit dispatch, CI) rely on.
PANE_SESSIONS_ENV = "IMPLEMENT_DRIVE_PANE_SESSIONS"
#: Default focus behaviour for pane-driven children: never steal the caller's
#: focus. ``--focus`` switches to each new child pane instead.
DEFAULT_PANE_FOCUS = False
#: Environment keys a pane child must receive explicitly (the recursion guard,
#: the worktree marker and — when present — the per-child lock token).
#: Deliberately an allow-list: the whole inherited environment is never
#: forwarded through ``pane split --env`` (leaked secrets in argv, noise).
PANE_CHILD_ENV_KEYS = (DRIVE_RECURSION_ENV, WORKTREE_ENV, ENV_LOCK_TOKEN)
#: The driving session's identity, which must never reach a child pane: the
#: child starts its own clean session (``_child_session_env`` removes them).
PANE_IDENTITY_ENV_KEYS = ("PI_SESSION_ID", "PI_SESSION_FILE")
#: Report action for a child whose per-work-item lock is held by a live
#: holder (another driver/session). The refused party performs no work and
#: reports the holder identity instead of racing it (SA-0MVQ5AXLF002PAQ3).
ACTION_SKIPPED_HELD = "skipped-held"
#: Transport labels recorded on every ``children_driven`` entry, so a
#: ``--json`` consumer can tell a pane-driven child from a headless one
#: (SA-0MVPHZ1CI0045QR0 AC5). ``pane_id`` is ``None`` for the headless path.
TRANSPORT_HEADLESS = "headless"
TRANSPORT_PANE = "pane"
#: Verdict title status (``herdr_pane`` vocabulary) for a pane drive retains:
#: ``attention`` (🚫) when the child reported ``blocked`` and needs a human
#: decision, ``note`` (⚠️) when its session failed but the pane is kept as
#: debugging evidence (SA-0MVPHZ1CI0045QR0 AC2/AC3).
PANE_VERDICT_BLOCKED = "attention"
PANE_VERDICT_FAILED = "note"

SLUG_MAX_LENGTH = 40
WORK_ITEM_ID_PATTERN = re.compile(r"^[A-Z]+-\w+$")

# ---------------------------------------------------------------------------
# Per-repo test-timeout configuration (SA-0MTXKIIRV009MJWG)
# ---------------------------------------------------------------------------
_TEST_CONFIG_FILE = ".pi/test-config.json"
_DEFAULT_TIMEOUT_PER_COMMAND = 600


def _resolve_test_timeout(cwd: str) -> int:
    """Return the per-command timeout (seconds) for the project at *cwd*.

    Reads ``.pi/test-config.json`` from the project root, extracts the
    ``timeoutPerCommand`` value, and falls back to 600 when the file is
    absent, the field is missing, or the value is invalid.

    This allows per-repo overrides (e.g. TCE sets 1500 to cover its
    ~19‑minute full suite) while keeping repos without the config file
    on the original 600‑second default.
    """
    root = Path(cwd).resolve()
    config_path = root / _TEST_CONFIG_FILE
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return _DEFAULT_TIMEOUT_PER_COMMAND
    if not isinstance(data, dict):
        return _DEFAULT_TIMEOUT_PER_COMMAND
    timeout = data.get("timeoutPerCommand")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return _DEFAULT_TIMEOUT_PER_COMMAND
    timeout = int(timeout)
    return timeout if timeout > 0 else _DEFAULT_TIMEOUT_PER_COMMAND

# State file name stored inside the worktree
STATE_FILE_NAME = ".implement_state.json"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ImplementState:
    """Persistent state for an in-progress implementation.

    Written to a JSON file inside the worktree so that the finish phase
    can resume deterministically.
    """

    work_item_id: str
    worktree_path: str
    repo_root: str
    parent_branch: str = DEFAULT_PARENT_BRANCH
    commit_msg: str = ""
    started_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_item_id": self.work_item_id,
            "worktree_path": self.worktree_path,
            "repo_root": self.repo_root,
            "parent_branch": self.parent_branch,
            "commit_msg": self.commit_msg,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImplementState:
        return cls(
            work_item_id=data["work_item_id"],
            worktree_path=data["worktree_path"],
            repo_root=data["repo_root"],
            parent_branch=data.get("parent_branch", DEFAULT_PARENT_BRANCH),
            commit_msg=data.get("commit_msg", ""),
            started_at=data.get("started_at", ""),
        )


# ---------------------------------------------------------------------------
# File-scoped signal state
# ---------------------------------------------------------------------------

_worktree_path_global: str | None = None
_work_item_id_global: str | None = None
_repo_root_global: str | None = None
#: The per-child lock held by this process for the lifetime of ``phase_start``.
#: Kept module-level so the signal handler can release it deterministically
#: before exiting, and so spawned processes inherit its ownership token.
_child_lock_global: WorkItemLock | None = None
#: The ``--keep-worktree`` decision of the running ``finish`` phase, kept
#: module-level so the signal handler defers removal for a session that
#: opted out of it (SA-0MVTE8XAJ002IUDR AC4).
_keep_worktree_global = False
_cleanup_done = False
#: Progress of the running ``finish`` phase (work item, worktree, branch, the
#: commit once it exists and whether it reached ``dev``). Kept module-level so
#: the SIGTERM/SIGINT handler can name the retained state — including the
#: pushed commit and the resume command — in its interruption comment instead
#: of releasing the item unexplained (SA-0MVXUSJJY009HMQ0 AC1/AC2). Cleared at
#: the start of every ``phase_finish`` and when its critical section is left.
_finish_progress_global: dict[str, Any] = {}


def _handle_signal_cleanup(signum: int) -> None:
    """Deterministic cleanup body for SIGTERM/SIGINT (does not exit).

    Split out of :func:`_signal_handler` so the cleanup contract — release
    the per-child lock, release the item's claim (status **and** assignee),
    never leave a claimed item unexplained, remove the worktree, restore repo
    state — is directly testable without terminating the interpreting
    process.

    The item is never released silently once a finish is in flight: a signal
    during ``phase_finish`` after the commit records the retained state —
    the published (or local) commit, the branch, whether the worktree
    survives and the command that resumes — through
    :func:`_record_interruption_comment`, deduplicated per commit
    (SA-0MVXUSJJY009HMQ0 AC1/AC2).

    Args:
        signum: The signal number that triggered the cleanup.
    """
    signame = signal.Signals(signum).name
    try:
        wt = _worktree_path_global
        wid = _work_item_id_global
        rr = _repo_root_global

        # A session that owns the worktree as its cwd must not lose it to an
        # interrupt either: the same deferral predicate as ``finish`` applies
        # (SA-0MVTE8XAJ002IUDR AC4). The item is released below regardless, so
        # an interrupted run never leaves an orphaned ``in_progress`` claim.
        # Resolved before the comment so the record names the tree's real fate.
        deferred, defer_reason = _should_defer_worktree_removal(
            bool(_keep_worktree_global)
        )

        # Reset the work item status FIRST: this is the critical invariant.
        # A failure in later cleanup steps must not prevent the item from
        # being released back to a valid terminal state.
        if wid:
            try:
                _safety_reset_if_in_progress(wid)
            except RuntimeError:
                LOG.error("Failed to reset work item %s status to open", wid)

            # A claim interrupted before the worktree exists must never be
            # silent: the operator needs to know the item was released and
            # why (SA-0MVQ5AXLF002PAQ3 AC2). The child lock is the phase
            # marker — only ``phase_start`` holds it, so a signal during
            # ``finish``/``abort`` never gets a misleading "start" comment.
            if _child_lock_global is not None and not (wt and Path(wt).exists()):
                try:
                    wl_add_comment(
                        wid,
                        f"Start phase interrupted by {signame} before the "
                        "worktree was created. The per-child lock was "
                        "released and the work item returned to open; "
                        f"re-run `implement.py start {wid}` to continue.",
                    )
                except Exception as exc:  # noqa: BLE001 - never block cleanup
                    LOG.error(
                        "Failed to record the %s interruption on %s: %s",
                        signame, wid, exc,
                    )
            elif _finish_progress_global.get("work_item_id") == wid:
                # An interrupted finish is never unexplained either: record
                # the retained state — the pushed commit, the branch, whether
                # the tree survives — and the command that resumes, so the
                # released item is recoverable and the re-run converges
                # (SA-0MVXUSJJY009HMQ0 AC1/AC2). ``_record_interruption_comment``
                # deduplicates on the marker plus commit, so a repeat signal or
                # a re-run never stacks records.
                try:
                    _record_interruption_comment(
                        wid,
                        f"{signame} during the finish phase",
                        # The tree survives only under the deferral predicate:
                        # the plain path removes it immediately below, and the
                        # record must say so (the resume then recreates it).
                        worktree_retained=bool(deferred),
                        worktree_path=wt,
                    )
                except Exception as exc:  # noqa: BLE001 - never block cleanup
                    LOG.error(
                        "Failed to record the %s interruption on %s: %s",
                        signame, wid, exc,
                    )

        if deferred:
            LOG.info(
                "Signal cleanup: worktree removal deferred (%s); the session "
                "still owns %s.",
                defer_reason,
                wt,
            )
        elif wt and Path(wt).exists():
            cleanup_worktree_processes(wt)
            _remove_worktree(wt)
        if rr:
            _restore_repo_state(rr)
    except Exception as exc:  # noqa: BLE001
        LOG.error("Cleanup handler error: %s", exc)
    finally:
        # Explicit release (the kernel also releases the flock when
        # ``os._exit`` closes the descriptor): no stale holder survives a
        # signal, and the release is observable in tests.
        _release_child_lock()
        # The suite may have been running against the finish gate's temporary
        # index; a signal must not leave that file (or its lock) behind.
        _remove_gate_index(_gate_index_global)


def _signal_handler(signum: int, frame: Any) -> None:
    """Global signal handler for SIGTERM/SIGINT during any phase.

    Runs deterministic cleanup: resets the work-item status (never leaves it
    in-progress), releases the per-child lock, terminates child processes,
    removes the worktree, and exits.

    Uses ``os._exit`` (not ``sys.exit``) so the status reset is authoritative:
    a ``sys.exit``-raised ``SystemExit`` would unwind through any active
    ``StatusLifecycle`` context manager, which would restore the original
    (in-progress) status and undo the reset.
    """
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True

    signame = signal.Signals(signum).name
    LOG.warning("Received signal %s, running deterministic cleanup...", signame)
    _handle_signal_cleanup(signum)
    LOG.info("Cleanup complete. Exiting due to %s.", signame)
    os._exit(2)


def _register_signal_handlers() -> None:
    """Register SIGTERM and SIGINT handlers."""
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)


def _store_signal_globals(
    worktree_path: str, work_item_id: str, repo_root: str
) -> None:
    """Store references for the signal handler."""
    global _worktree_path_global, _work_item_id_global, _repo_root_global
    _worktree_path_global = worktree_path
    _work_item_id_global = work_item_id
    _repo_root_global = repo_root


def _set_keep_worktree_global(enabled: bool) -> None:
    """Record the ``--keep-worktree`` decision for the signal handler."""
    global _keep_worktree_global
    _keep_worktree_global = bool(enabled)


def _set_finish_progress(**fields: Any) -> None:
    """Record finish-phase progress for the interruption comment.

    Idempotent: each call merges the supplied fields into the module-level
    context, so the phase can accumulate (worktree, branch) at the start and
    the commit/push facts as they become true without re-stating the rest.

    Args:
        **fields: Progress facts (``work_item_id``, ``worktree_path``,
            ``branch``, ``commit``, ``pushed``).
    """
    _finish_progress_global.update(fields)


def _clear_finish_progress() -> None:
    """Forget finish-phase progress once the phase leaves its critical section.

    A cleared context means a later signal has no half-stated finish to
    report: either the run had not reached the commit (nothing was published)
    or the phase already converged.
    """
    _finish_progress_global.clear()


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def run_cmd(
    cmd: list[str],
    cwd: str | None = None,
    capture: bool = True,
    check: bool = False,
    timeout: int = 300,
    env: dict[str, str] | None = None,
    start_new_session: bool = False,
    keep_git_index: bool = False,
) -> subprocess.CompletedProcess:
    """Run a shell command and return the result.

    Args:
        cmd: Command as a list of strings.
        cwd: Working directory (default: current).
        capture: If True, capture stdout/stderr.
        check: If True, raise CalledProcessError on non-zero exit.
        timeout: Timeout in seconds.
        env: Optional environment variable overrides.
        start_new_session: If True, spawn the child in its own session /
            process group so process-group kills performed inside it can
            never reach the caller or its siblings
            (SA-0MVJC5KNT0044L2B).
        keep_git_index: If True, inherit an ambient ``GIT_INDEX_FILE``. By
            default it is dropped so the finish gate's own git commands (and
            any non-suite command it spawns) always act on the real index
            while the gate exports a temporary index for the suite
            (SA-0MVQHVGFR006W8LG). Suite runners opt in explicitly.

    Returns:
        ``subprocess.CompletedProcess``

    Raises:
        ``subprocess.TimeoutExpired`` on timeout.
        ``subprocess.CalledProcessError`` if *check* is True and exit != 0.
    """
    LOG.debug("Running: %s (cwd=%s)", " ".join(cmd), cwd or os.getcwd())
    child_env = dict(os.environ)
    if not keep_git_index:
        child_env.pop("GIT_INDEX_FILE", None)
    child_env.update(env or {})
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=capture,
        text=True,
        check=check,
        timeout=timeout,
        env=child_env,
        start_new_session=start_new_session,
    )


def format_json_output(data: dict[str, Any]) -> str:
    """Format dict as pretty-printed JSON."""
    return json.dumps(data, indent=2, default=str)


def slug_from_title(title: str) -> str:
    """Create a short slug from a work-item title.

    Args:
        title: The work-item title string.

    Returns:
        A kebab-case slug truncated to ``SLUG_MAX_LENGTH`` characters.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
    return slug[:SLUG_MAX_LENGTH].rstrip("-")


def worktree_path_for(work_item_id: str, slug: str) -> str:
    """Compute the canonical worktree path.

    The returned path is **relative to the main checkout root**, not to the
    current working directory. Callers that need an absolute path must join
    it onto the main checkout root (``_get_repo_root()``) rather than calling
    ``Path(wt_path).resolve()`` while ``cwd`` may be inside a linked worktree
    — otherwise the path nests under the worktree and the registered tree is
    missed (SA-0MVXUGXS5006M3HF).

    Args:
        work_item_id: The work item ID.
        slug: Short slug for the branch name.

    Returns:
        Relative path string like ``.worklog/worktrees/wl-<id>-<slug>``.
    """
    return f"{DEFAULT_WORKTREE_DIR}/wl-{work_item_id}-{slug}"


def branch_name_for(work_item_id: str, slug: str) -> str:
    """Compute the canonical feature branch name.

    Args:
        work_item_id: The work item ID.
        slug: Short slug for the branch name.

    Returns:
        Branch name string like ``wl-<id>-<slug>``.
    """
    return f"wl-{work_item_id}-{slug}"


# ---------------------------------------------------------------------------
# Worklog interaction helpers
# ---------------------------------------------------------------------------


def wl_show(work_item_id: str) -> dict[str, Any]:
    """Fetch a work item by ID as JSON.

    Args:
        work_item_id: The work item ID.

    Returns:
        Parsed JSON dict from ``wl show``.
    """
    cmd = ["wl", "show", work_item_id, "--json"]
    cmd[1:1] = resolve_worklog_flags(cmd)
    result = run_cmd(cmd, check=False)
    if result.returncode != 0:
        LOG.error("Failed to fetch work item %s: %s", work_item_id, result.stderr.strip())
        return {}
    try:
        data = json.loads(result.stdout.strip())
        if isinstance(data, dict) and "workItem" in data:
            return data["workItem"]
        return data
    except (json.JSONDecodeError, ValueError) as exc:
        LOG.error("Invalid JSON from wl show: %s", exc)
        return {}


def wl_show_children(work_item_id: str) -> list[dict[str, Any]]:
    """Fetch a work item's children as a list of dicts.

    Uses ``wl show <id> --children --json`` and returns the ``children``
    array (empty when the item has no children or the fetch fails).

    Args:
        work_item_id: The parent work item ID.

    Returns:
        List of child work-item dicts (may be empty).
    """
    cmd = ["wl", "show", work_item_id, "--children", "--json"]
    cmd[1:1] = resolve_worklog_flags(cmd)
    result = run_cmd(cmd, check=False)
    if result.returncode != 0:
        LOG.error(
            "Failed to fetch children of %s: %s",
            work_item_id,
            result.stderr.strip(),
        )
        return []
    try:
        data = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, ValueError) as exc:
        LOG.error("Invalid JSON from wl show --children: %s", exc)
        return []
    if not isinstance(data, dict):
        return []
    children = data.get("children")
    return children if isinstance(children, list) else []


def wl_dep_blockers(work_item_id: str) -> list[dict[str, Any]]:
    """Fetch a work item's outbound (depends-on) dependency edges.

    Uses ``wl dep list <id> --json`` and returns the ``outbound`` array —
    the items this work item is blocked by / depends on. Empty when the
    item has no dependencies or the fetch fails.

    Args:
        work_item_id: The work item ID.

    Returns:
        List of outbound dependency-edge dicts (may be empty).
    """
    cmd = ["wl", "dep", "list", work_item_id, "--json"]
    cmd[1:1] = resolve_worklog_flags(cmd)
    result = run_cmd(cmd, check=False)
    if result.returncode != 0:
        LOG.error(
            "Failed to fetch dependencies of %s: %s",
            work_item_id,
            result.stderr.strip(),
        )
        return []
    try:
        data = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, ValueError) as exc:
        LOG.error("Invalid JSON from wl dep list: %s", exc)
        return []
    if not isinstance(data, dict):
        return []
    outbound = data.get("outbound")
    return outbound if isinstance(outbound, list) else []


def wl_add_comment(work_item_id: str, comment: str) -> bool:
    """Add a comment to a work item.

    Args:
        work_item_id: The work item ID.
        comment: The comment text.

    Returns:
        True if the comment was added.
    """
    cmd = [
        "wl", "comment", "add", work_item_id,
        "--comment", comment, "--author", "implement",
    ]
    cmd[1:1] = resolve_worklog_flags(cmd)
    result = run_cmd(cmd, check=False)
    if result.returncode != 0:
        LOG.warning("Failed to add comment to %s: %s", work_item_id, result.stderr.strip())
        return False
    return True


def _assert_terminal_status(work_item_id: str) -> dict[str, Any]:
    """Verify the store holds the terminal transition after a successful finish.

    The terminal ``status=completed``/``stage=in_review`` write is issued by
    ``StatusLifecycle.__exit__`` on **both** the push-with-diff path and the
    no-op-push (no-diff) path — a branch with no diff from ``origin/dev``
    still reaches the context manager and the write persists. This
    read-back turns a silently lost or overwritten lifecycle write into a
    loud finish failure instead of a green report: the recorded symptom on
    OSL-0MVMB44C9008IXZX was exactly that — ``finish`` reported success
    while the item was still ``open`` (OSL-0MVME1PI5005WU58).

    The assertion runs only after Step 6's confirmed successful push (and
    after ``StatusLifecycle.__exit__`` issued the write), so a genuine push
    failure can never surface as ``in_review``. The regression pin is
    ``skill/implement/tests/test_finish_no_diff_status.py``.

    Args:
        work_item_id: The work item ID.

    Returns:
        Dict with ``success`` (True), ``status`` and ``stage`` as read back
        from the main-checkout store.

    Raises:
        RuntimeError: when the store does not hold terminal state — the
            finish phase must then fail (and the item be released) instead
            of reporting success.
    """
    try:
        data = StatusLifecycle.show(work_item_id)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Status read-back failed after a successful finish: {exc}"
        ) from exc
    wi = data.get("workItem", {}) if isinstance(data, dict) else {}
    status = wi.get("status", "")
    stage = wi.get("stage", "")
    if status == "completed" and stage == "in_review":
        return {"success": True, "status": status, "stage": stage}
    raise RuntimeError(
        f"Finish reported success but the main-checkout store does not hold "
        f"the terminal transition for {work_item_id}: status={status!r}, "
        f"stage={stage!r} (expected completed / in_review). The lifecycle "
        f"write was lost or overwritten — investigate before re-running "
        f"finish, or complete the transition by hand if the store was "
        f"restored from a stale snapshot."
    )


def _safety_reset_if_in_progress(work_item_id: str) -> None:
    """Reset a work item to ``open`` ONLY if it is currently in-progress.

    Guards against clobbering items that already reached a valid terminal
    state (``open``/``blocked``/``completed``) when a late, unrelated error
    or signal occurs (e.g. after a successful finish phase).

    The reset also **clears the assignee** (``assignee=""``): a non-empty
    assignee is a scheduling hold, so a killed session that only reclaimed the
    status would still leave the item invisible to ``wl next``. This is the
    durable release for the signal path (SIGTERM/SIGINT); the killed-by-
    timeout counterpart is :func:`_release_orphaned_child_claim`
    (SA-0MVVF75ZA007XNKB).

    Args:
        work_item_id: The work item ID.
    """
    try:
        data = StatusLifecycle.show(work_item_id)
    except RuntimeError:
        LOG.warning("Could not fetch %s; skipping safety reset", work_item_id)
        return
    wi = data.get("workItem", {})
    current = wi.get("status", "")
    if current in ("in-progress", "in_progress"):
        # Durable-write reset: this safety path runs outside any StatusLifecycle
        # context (signal/error unwinding), so the helper's release-by-default
        # exit hook does not apply — the claim is released explicitly here.
        StatusLifecycle.update_status(work_item_id, "open", assignee="")
        LOG.info("Safety reset: %s status in-progress -> open", work_item_id)
    else:
        LOG.info("Safety reset skipped: %s status is %r", work_item_id, current)


def _work_item_comment_bodies(work_item_id: str) -> list[str]:
    """Return the comment bodies recorded on a work item (fail-open).

    Read through :meth:`StatusLifecycle.show` (``wl show --json``), which
    returns the full payload — ``workItem``, ``comments``, ``children`` — so
    the idempotence checks below need no extra ``wl`` invocation. An
    unreadable store, a malformed payload or a harness stub without comments
    yields an empty list: the caller then records its comment rather than
    silently skipping it (the safe direction for a recovery record).

    Args:
        work_item_id: The work item whose comments to read.

    Returns:
        The comment bodies, in store order (empty on any read failure).
    """
    try:
        data = StatusLifecycle.show(work_item_id)
    except RuntimeError as exc:
        LOG.warning("Could not read the comments of %s: %s", work_item_id, exc)
        return []
    comments = data.get("comments") if isinstance(data, dict) else None
    if not isinstance(comments, list):
        return []
    bodies: list[str] = []
    for entry in comments:
        if not isinstance(entry, dict):
            continue
        body = entry.get("comment")
        if isinstance(body, str):
            bodies.append(body)
    return bodies


def _comment_already_recorded(work_item_id: str, *needles: str) -> bool:
    """Whether a comment containing **every** needle already exists.

    The idempotence primitive for the re-run convergence path
    (SA-0MVXUSJJY009HMQ0 AC4) and the interruption record (AC2): a stable
    marker plus the commit hash identifies the record, so re-running ``finish``
    in a fresh process never duplicates it.

    Args:
        work_item_id: The work item whose comments to search.
        *needles: Substrings that must all appear in one comment body.

    Returns:
        True when such a comment exists; False otherwise (including an
        unreadable store — never suppress a recovery record silently).
    """
    if not needles:
        return False
    for body in _work_item_comment_bodies(work_item_id):
        if all(needle in body for needle in needles):
            return True
    return False


def _interruption_state_comment(
    work_item_id: str,
    reason: str,
    progress: dict[str, Any] | None = None,
    *,
    worktree_retained: bool = False,
    worktree_path: str | None = None,
) -> str:
    """Build the comment that names an interruption's retained state.

    The record answers the three questions an operator (or the next agent)
    needs after an interrupted run (SA-0MVXUSJJY009HMQ0 AC1/AC2): what state
    was retained, whether any work was published, and which command resumes.

    Args:
        work_item_id: The interrupted work item.
        reason: Human-readable interruption reason (signal or failure).
        progress: The finish-phase progress facts; defaults to the module-level
            context.
        worktree_retained: Whether the worktree survives the interruption.
        worktree_path: The worktree path, when known.

    Returns:
        The comment body (starts with :data:`INTERRUPTION_COMMENT_MARKER`).
    """
    facts = _finish_progress_global if progress is None else progress
    commit = str(facts.get("commit") or "")
    pushed = bool(facts.get("pushed"))
    branch = str(facts.get("branch") or "")
    path = str(worktree_path or facts.get("worktree_path") or "")

    lines = [
        f"{INTERRUPTION_COMMENT_MARKER}: {reason}.",
        "Retained state:",
        (
            "- Status: open, assignee cleared (the claim is released). The item "
            "is not in_review, so it is not an audit-dispatch candidate."
        ),
        "- Stage: unchanged — the terminal transition never ran.",
    ]
    if commit:
        published = "pushed to dev" if pushed else "committed locally, not pushed"
        target = f" on branch {branch}" if branch else ""
        lines.append(f"- Commit: {commit} ({published}{target}).")
    else:
        lines.append(
            "- Commit: none — no commit was created before the interruption."
        )
    if path:
        if worktree_retained:
            lines.append(f"- Worktree: retained at {path} (resume onto it directly).")
        else:
            lines.append(
                f"- Worktree: removed ({path}); the work that reached dev is "
                "unaffected."
            )
    lines.append("Resume with:")
    if path and not worktree_retained:
        lines.append(
            "  python3 skill/implement/scripts/implement.py start "
            f"{work_item_id}"
        )
    lines.append(
        "  python3 skill/implement/scripts/implement.py finish "
        f"{work_item_id}"
    )
    lines.append(
        "Re-running finish is idempotent: it converges to completed / "
        "in_review, releases the assignee and does not duplicate this comment "
        "or the completion comment."
    )
    return "\n".join(lines)


def _record_interruption_comment(
    work_item_id: str,
    reason: str,
    *,
    worktree_retained: bool = False,
    worktree_path: str | None = None,
) -> dict[str, Any]:
    """Record an interruption's retained state, at most once per commit.

    Never raises: a comment failure must not pre-empt the status release that
    precedes it. The outcome is returned so the caller can surface it in its
    report.

    Args:
        work_item_id: The interrupted work item.
        reason: Human-readable interruption reason.
        worktree_retained: Whether the worktree survives the interruption.
        worktree_path: The worktree path, when known.

    Returns:
        Dict with ``recorded`` (bool), ``deduplicated`` (bool) and ``commit``.
    """
    commit = str(_finish_progress_global.get("commit") or "")
    commit_needle = f"Commit: {commit}" if commit else "Commit: none"
    if _comment_already_recorded(
        work_item_id, INTERRUPTION_COMMENT_MARKER, commit_needle
    ):
        LOG.info(
            "Interruption comment for %s (commit %s) already recorded; not "
            "duplicating it.",
            work_item_id,
            commit or "none",
        )
        return {"recorded": False, "deduplicated": True, "commit": commit}
    body = _interruption_state_comment(
        work_item_id,
        reason,
        worktree_retained=worktree_retained,
        worktree_path=worktree_path,
    )
    recorded = wl_add_comment(work_item_id, body)
    return {"recorded": bool(recorded), "deduplicated": False, "commit": commit}


# ---------------------------------------------------------------------------
# Per-work-item child lock (SA-0MVQ5AXLF002PAQ3)
#
# The flock primitive lives in ``shared.work_item_lock``; this section is the
# implement-side integration: the choke point that owns it, the read-only
# probe used for reporting, and the ownership token propagated to spawned
# processes.
# ---------------------------------------------------------------------------


def _child_lock_dir(work_item_id: str) -> Path | None:
    """Resolve the worklog **store** directory that owns the child lock.

    Single, patchable resolution point: the lock must live under
    ``<worklog>/locks/`` (never inside a worktree, which can be removed
    mid-flight).

    ``IMPLEMENT_CHILD_LOCK_DIR`` overrides the store explicitly. It exists
    for **test isolation**: ``phase_start`` takes a real ``flock`` before any
    of its test-mocked steps, and synthetic test ids share the repository's
    own id prefix, so an un-isolated suite would write (and race) real lock
    files inside the developer's ``.worklog/locks`` (SA-0MVWJ6U3J003TLRZ).
    The suites set it to a per-test throwaway directory; it is unset in every
    production invocation, where the worklog store is authoritative.

    Args:
        work_item_id: The work item whose lock directory is resolved.

    Returns:
        The worklog store path, or ``None`` when it cannot be resolved.
    """
    override = os.environ.get(ENV_CHILD_LOCK_DIR)
    if override:
        return Path(override).expanduser()
    try:
        return resolve_worklog_dir(work_item_id)
    except RuntimeError:
        return None


def _new_child_lock(work_item_id: str) -> WorkItemLock:
    """Build the per-work-item lock handle for *work_item_id*.

    The ownership token comes from ``IMPLEMENT_CHILD_LOCK_TOKEN`` when the
    process is part of an existing holder chain (driver → ``parent``/``start``
    subprocesses → driven child session); otherwise the lock generates one.

    Args:
        work_item_id: The work item whose critical section is serialised.

    Returns:
        A not-yet-acquired :class:`WorkItemLock`.
    """
    worklog_dir = _child_lock_dir(work_item_id)
    kwargs: dict[str, Any] = {
        "agent": os.environ.get("IMPLEMENT_AGENT_NAME") or "implement",
        "session_id": os.environ.get("PI_SESSION_ID") or f"pid-{os.getpid()}",
    }
    if worklog_dir is not None:
        kwargs["worklog_dir"] = worklog_dir
    return WorkItemLock(work_item_id, **kwargs)


def _set_child_lock(lock: WorkItemLock) -> None:
    """Register *lock* as this process's held child lock (signal-safe)."""
    global _child_lock_global
    _child_lock_global = lock


def _release_child_lock() -> None:
    """Release and forget this process's held child lock (idempotent)."""
    global _child_lock_global
    lock = _child_lock_global
    _child_lock_global = None
    if lock is not None:
        lock.release()


def _child_lock_token(base: dict[str, str] | None = None) -> str | None:
    """Return the explicit lock ownership token of the current holder chain.

    Prefers the token of the lock this process actually holds; falls back to
    the inherited :data:`ENV_LOCK_TOKEN` of an enclosing holder chain. Never
    derives ownership from a pid — pid equality alone is deliberately not an
    ownership signal (SA-0MOABYW contract, AC4).

    Args:
        base: Environment mapping to inspect when no lock is held locally
            (defaults to ``os.environ``).

    Returns:
        The token string, or ``None`` when this process is not part of a
        holder chain.
    """
    if _child_lock_global is not None:
        return _child_lock_global.token
    env = base if base is not None else os.environ
    return env.get(ENV_LOCK_TOKEN) or None


def _read_child_lock_metadata(path: Path) -> dict[str, Any] | None:
    """Read lock-file owner metadata (best-effort; ``None`` when unreadable)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _probe_child_lock(work_item_id: str) -> dict[str, Any] | None:
    """Return the live holder's metadata when *work_item_id*'s lock is held.

    Read-only by construction: it opens an **existing** lock file, takes a
    non-blocking ``flock`` probe and releases it immediately. It never
    creates, rewrites or truncates the lock file, so classifying a child has
    no side effects (a refused contender must leave a byte-identical store
    behind).

    A holder chain that propagated its ownership token is not a *blocking*
    holder: the probe reports self-ownership as free, exactly like the lock
    primitive's re-entrancy rule (never a pid-equality heuristic).

    Ambiguous states (held but unreadable metadata) fail closed: they report
    a holder with an ``unknown`` identity rather than racing a live writer.

    Args:
        work_item_id: The child work item to probe.

    Returns:
        Holder metadata when a live holder exists, else ``None``.
    """
    if not work_item_id or fcntl is None:
        return None
    store = _child_lock_dir(work_item_id)
    if store is None:
        return None
    path = lock_file_path(work_item_id, store)
    if not path.is_file():
        return None
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                return None
            metadata = _read_child_lock_metadata(path)
            if metadata is not None and metadata.get("token") == _child_lock_token():
                return None  # our own holder chain: re-entrant, not blocked
            if metadata is None:
                return {
                    "agent": "unknown",
                    "pid": None,
                    "session_id": "unknown",
                    "started_at": "unknown",
                }
            return metadata
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
    finally:
        try:
            os.close(fd)
        except OSError:  # pragma: no cover - defensive
            pass


def _describe_child_lock_holder(holder: dict[str, Any] | None) -> str:
    """Human-readable holder identity for a ``skipped-held`` report."""
    if not holder:
        return "an unknown holder (lock file unreadable)"
    return (
        f"{holder.get('agent', '?')}@pid {holder.get('pid', '?')} "
        f"(session {holder.get('session_id', '?')}, "
        f"since {holder.get('started_at', '?')})"
    )


#: Holder metadata keys surfaced for a reclaimed stale lock. The ownership
#: token is deliberately excluded: it must never be copied into a report.
RECLAIM_REPORTED_KEYS = ("agent", "pid", "session_id", "started_at")


def _reclaim_warning(work_item_id: str, holder: dict[str, Any] | None) -> str:
    """Human-readable warning naming the reclaimed (dead) lock holder."""
    return (
        f"Reclaimed a stale per-child lock for {work_item_id} from "
        f"{_describe_child_lock_holder(holder)}; the recorded holder pid is "
        "dead, so the lock was taken over (the previous session crashed)."
    )


def _record_child_lock_reclamation(
    report: dict[str, Any],
    work_item_id: str,
    lock_result: Any,
) -> str:
    """Surface a reclaimed stale holder in *report* (never only in the log).

    The parent epic ``SA-0MVOABYW00004978`` requires the reclamation warning
    to be logged **and** surfaced in the report, so an operator (or a
    ``--json`` consumer) can see who was reclaimed without reading stderr
    (``SA-0MVUX6Q4S006KII3``).

    Args:
        report: The caller's report dict (mutated in place).
        work_item_id: The work item whose lock was reclaimed.
        lock_result: The :class:`LockResult` carrying ``reclaimed``/``path``.

    Returns:
        The warning string that was appended to ``report["warnings"]``.
    """
    holder = getattr(lock_result, "reclaimed", None) or {}
    path = getattr(lock_result, "path", None)
    report["lock_reclaimed"] = True
    report["reclaimed_from"] = {
        key: holder.get(key) for key in RECLAIM_REPORTED_KEYS
    }
    report["lock_path"] = str(path) if path is not None else ""
    warning = _reclaim_warning(work_item_id, holder)
    report.setdefault("warnings", []).append(warning)
    return warning


def _forward_child_lock_reclamation(
    report: dict[str, Any],
    child_id: str,
    child_report: dict[str, Any] | None,
) -> bool:
    """Copy a child's reclaimed-lock evidence into a parent/drive report.

    ``phase_parent``/``phase_drive`` consumers must see a reclamation that
    happened while the child was started, so it is forwarded (with the child
    named) instead of staying buried in the child's own report or log.

    Args:
        report: The parent/drive report (mutated in place).
        child_id: The child the evidence belongs to (may be empty).
        child_report: The child ``start`` report, if any.

    Returns:
        True when reclamation evidence was forwarded.
    """
    if not child_report or not child_report.get("lock_reclaimed"):
        return False
    report["lock_reclaimed"] = True
    report["reclaimed_from"] = child_report.get("reclaimed_from")
    report["lock_path"] = child_report.get("lock_path", "")
    if child_id:
        report["reclaimed_children"] = sorted(
            set(report.get("reclaimed_children", [])) | {child_id}
        )
    warnings = report.setdefault("warnings", [])
    forwarded = list(child_report.get("warnings") or [])
    if not forwarded:
        forwarded = [
            (
                f"Reclaimed a stale per-child lock for {child_id} from "
                f"{_describe_child_lock_holder(child_report.get('reclaimed_from'))}."
            )
        ]
    for warning in forwarded:
        if warning not in warnings:
            warnings.append(warning)
    return True


def _skipped_held_report(
    report: dict[str, Any],
    work_item_id: str,
    holder: dict[str, Any] | None,
    lock_path: Path | None,
) -> dict[str, Any]:
    """Populate *report* for a contender that could not acquire the lock.

    A refused ``start`` performs **no** store write: no claim, no
    status/assignee mutation, no comment, no worktree create/remove and no
    lock-metadata rewrite. It reports the holder identity instead.
    """
    report["success"] = False
    report["action"] = ACTION_SKIPPED_HELD
    report["held_by"] = holder
    report["lock_path"] = str(lock_path) if lock_path is not None else ""
    report["message"] = (
        f"Work item {work_item_id} is held by {_describe_child_lock_holder(holder)}. "
        "Another implementation session owns its critical section; no "
        "claim, worktree or file write was performed. Re-run once the holder "
        "finishes (or reclaim a stale lock when its pid is dead)."
    )
    return report


def _check_and_evaluate_risk_effort(
    work_item_id: str, work_item: dict[str, Any],
) -> dict[str, Any]:
    """Check risk/effort estimates; run effort-and-risk if missing (SA-0MTTSWHQE0072N9J).

    When a plan_complete item lacks ``risk`` and/or ``effort`` fields, this
    function runs the effort-and-risk skill to produce and persist estimates
    via ``wl update``.  Items that already have both fields are passed
    through unchanged — no gate error, no behaviour change.

    Args:
        work_item_id: The work-item identifier.
        work_item: The work-item dict from ``wl show``.

    Returns:
        A dict with keys ``success`` (bool), ``estimates_provided`` (bool),
        ``effort`` (str|None), ``risk`` (str|None) and optional ``error``.
    """
    risk = work_item.get("risk") or ""
    effort = work_item.get("effort") or ""
    stage = work_item.get("stage", "")

    # Only evaluate items that are plan_complete (the gate only applies here)
    if stage != "plan_complete":
        LOG.info(
            "Risk/effort evaluation skipped: stage is %r (not plan_complete)",
            stage,
        )
        return {
            "success": True,
            "estimates_provided": False,
            "reason": f"stage={stage}",
        }

    # Both fields present — no evaluation needed (no behaviour change)
    if risk and effort:
        LOG.info(
            "Risk/effort already set: risk=%r effort=%r",
            risk, effort,
        )
        return {
            "success": True,
            "estimates_provided": False,
            "reason": "estimates already present",
        }

    # ── Missing estimates: run the effort-and-risk evaluation ────────────
    LOG.warning(
        "Missing risk/effort on plan_complete item %s "
        "(risk=%r effort=%r) — running evaluation.",
        work_item_id, risk, effort,
    )

    # Fetch children for a better estimate (reuses the shared helper so the
    # cross-repo worklog resolution stays consistent — OSL-0MUI78ROQ005B37N).
    children = []
    for child in wl_show_children(work_item_id):
        children.append({
            "id": child.get("id", ""),
            "title": child.get("title", ""),
            "probability": 2,
            "impact": 1,
        })

    # Build the payload for the orchestrator
    payload = {
        "o": 2.0,
        "m": 4.0,
        "p": 8.0,
        "overheads": {
            "coordination": 1.0,
            "review": 1.0,
            "testing": 1.0,
            "risk_buffer": 1.0,
        },
        "parent": {"probability": 2.1, "impact": 2.1},
        "children": children,
        "certainty": 85.0,
        "assumptions": [
            "Default O/M/P used because no prior estimates exist",
            "Standard overheads applied",
        ],
        "unknowns": [
            "Exact scope may differ from initial understanding",
        ],
        "issue_id": work_item_id,
    }

    # Locate the orchestrator script
    skill_root = str(Path(__file__).resolve().parents[2])
    orchestrator = os.path.join(
        skill_root, "effort-and-risk", "scripts", "orchestrate_estimate.py",
    )

    if not os.path.isfile(orchestrator):
        LOG.error(
            "effort-and-risk orchestrator not found at %s — "
            "item will be blocked.",
            orchestrator,
        )
        return {
            "success": False,
            "estimates_provided": False,
            "error": "orchestrator not found",
        }

    # Run the orchestrator
    try:
        proc = subprocess.run(
            ["python3", orchestrator],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
        if proc.returncode != 0:
            LOG.error(
                "effort-and-risk evaluation failed (rc=%d): %s",
                proc.returncode, proc.stderr.strip(),
            )
            return {
                "success": False,
                "estimates_provided": False,
                "error": f"orchestrator failed: {proc.stderr.strip()}",
            }

        # The orchestrator updates the work item via wl update internally.
        # Parse the output to report what was produced.
        try:
            result = json.loads(proc.stdout.strip())
        except json.JSONDecodeError:
            result = {}

        # Check whether the update succeeded
        update_ok = result.get("update_result", {}).get("success", False)
        if not update_ok:
            LOG.error(
                "Failed to persist estimates for %s: %s",
                work_item_id,
                result.get("update_result", {}),
            )
            return {
                "success": False,
                "estimates_provided": False,
                "error": "failed to persist estimates via wl update",
            }

        wl_effort = result.get("effort", {}).get("tshirt", "")
        wl_risk = result.get("risk", {}).get("level", "")
        # Map risk level to wl label
        risk_map = {"Low": "Low", "Medium": "Medium", "High": "High", "Critical": "Severe", "Severe": "Severe"}
        wl_risk_label = risk_map.get(wl_risk, "Medium")

        LOG.info(
            "Risk/effort evaluated and persisted: risk=%s effort=%s",
            wl_risk_label, wl_effort,
        )
        return {
            "success": True,
            "estimates_provided": True,
            "risk": wl_risk_label,
            "effort": wl_effort,
        }

    except subprocess.TimeoutExpired:
        LOG.error(
            "effort-and-risk evaluation timed out for %s",
            work_item_id,
        )
        return {
            "success": False,
            "estimates_provided": False,
            "error": "evaluation timed out (5 min)",
        }
    except Exception as exc:  # noqa: BLE001
        LOG.error(
            "Unexpected error running effort-and-risk for %s: %s",
            work_item_id, exc,
        )
        return {
            "success": False,
            "estimates_provided": False,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Parent-recursion helpers
# ---------------------------------------------------------------------------

TERMINAL_STATUSES = {"in_review", "completed", "done", "deleted"}


def _is_terminal_status(status: str) -> bool:
    """Whether a work-item status is terminal for parent advancement.

    A terminal child is never re-implemented and counts toward parent
    advancement (AC-4). ``in_review``/``completed``/``done`` are terminal;
    ``open``/``blocked``/``in_progress`` are not.

    Soft-deleted items (status ``deleted``) are also treated as terminal:
    they are spurious/removed and can never be implemented, so they must
    never be claimed by ``phase_parent`` nor block parent advancement
    (SA-0MSRVN28E009OL5S).

    Args:
        status: The work-item status string.

    Returns:
        True if the status is terminal.
    """
    return status in TERMINAL_STATUSES


def _is_terminal_child(child: dict[str, Any]) -> bool:
    """Whether a child work item is terminal for parent-recursion purposes.

    A child is terminal when either its status is terminal
    (:func:`_is_terminal_status`) or it has been soft-deleted
    (``deletedBy`` set). ``wl delete`` sets ``deletedBy`` and may leave the
    status untouched (e.g. ``open``); such items can never be implemented,
    so they must be skipped and must never block parent advancement
    (SA-0MUTWB8BA003J0V6).

    Args:
        child: A child work-item dict from ``wl show --children``.

    Returns:
        True if the child is terminal.
    """
    if str(child.get("deletedBy", "") or "").strip():
        return True
    return _is_terminal_status(str(child.get("status", "")))


#: ``parent_blocked_reason`` values for the invoked item's blocked verdict.
PARENT_BLOCKED_REASON_DEPENDENCY = "outbound-dependency"
PARENT_BLOCKED_REASON_STATUS = "status-blocked"

#: Per-child action used when the invoked parent itself is blocked.
ACTION_BLOCKED_PARENT = "blocked-parent"


def _work_item_prefix(work_item_id: str) -> str:
    """Return the project prefix of a work-item id (``SA`` for ``SA-0MVS1Q``).

    The prefix before the first ``-`` identifies the worklog project, so two
    ids with different prefixes belong to different projects.

    Args:
        work_item_id: A work-item id.

    Returns:
        The upper-cased prefix (empty when the id has no ``-``).
    """
    return work_item_id.split("-", 1)[0].strip().upper()


def _is_same_project(owner_id: str, other_id: str) -> bool:
    """Whether two work-item ids belong to the same worklog project.

    A dependency edge to an item with a different prefix is a cross-project
    coordination note rather than an orchestrator gate — the documented
    assumption introduced with parent recursion (SA-0MSQBM2FK005NW1T).

    Args:
        owner_id: The item whose edges are being evaluated.
        other_id: The id of the item on the other end of the edge.

    Returns:
        True when both ids share a non-empty project prefix.
    """
    prefix = _work_item_prefix(owner_id)
    return bool(prefix) and prefix == _work_item_prefix(other_id)


def _blocker_payload(blocker: dict[str, Any]) -> dict[str, str]:
    """Render a dependency edge as a compact, JSON-friendly description.

    Args:
        blocker: An outbound edge dict from ``wl dep list``.

    Returns:
        A dict with ``id``, ``status`` and ``title`` string fields.
    """
    return {
        "id": str(blocker.get("id", "")),
        "status": str(blocker.get("status", "")),
        "title": str(blocker.get("title", "")),
    }


def _format_blockers(blockers: list[dict[str, str]]) -> str:
    """Render a blocker payload list as ``id (status), …`` for messages."""
    return ", ".join(
        f"{b.get('id', '?')} ({b.get('status') or 'unknown'})" for b in blockers
    )


def _outbound_gate(
    work_item_id: str,
    blockers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate the invoked item's **own** outbound (depends-on) blockers.

    ``phase_parent``/``phase_drive`` used to build their dependency graph
    only from the children's edges, so a producer's sequencing hold recorded
    on the parent itself was silently overridden and the first startable
    child's worktree was created mid-flight (SA-0MVS1QS9J009DGPT). The same
    gate applies to a leaf item in ``phase_start``.

    Terminal blockers — ``in_review``/``completed``/``done``/``deleted``, and
    soft-deleted items (``deletedBy`` set) — never gate, matching
    :func:`_is_terminal_child`. Cross-project blockers (a different id prefix)
    are reported as coordination notes and never gate
    (SA-0MSQBM2FK005NW1T).

    Args:
        work_item_id: The invoked work item.
        blockers: The item's outbound edges; fetched via
            :func:`wl_dep_blockers` when omitted.

    Returns:
        ``{"blocked": bool, "blockers": [...], "coordination_blockers":
        [...]}`` where ``blockers`` holds the unmet same-project edges that
        gate starting and ``coordination_blockers`` the unmet cross-project
        edges (informational only).
    """
    edges = wl_dep_blockers(work_item_id) if blockers is None else blockers
    unmet = [edge for edge in edges if not _is_terminal_child(edge)]
    blocking = [
        edge
        for edge in unmet
        if _is_same_project(work_item_id, str(edge.get("id", "")))
    ]
    coordination = [edge for edge in unmet if edge not in blocking]
    return {
        "blocked": bool(blocking),
        "blockers": [_blocker_payload(edge) for edge in blocking],
        "coordination_blockers": [
            _blocker_payload(edge) for edge in coordination
        ],
    }


def _classify_child(child: dict[str, Any]) -> str:
    """Classify a child for the parent-recursion plan.

    Returns one of:
      - ``"skip-terminal"`` — already in a terminal state; never re-implemented.
      - ``"skipped-held"`` — a live holder owns the child's per-work-item
        lock (another driver/session); reported, never raced.
      - ``"skip-in-progress"`` — claimed by another agent; reported, not clobbered.
      - ``"implement"`` — the standard workflow should run on this child.

    Precedence is deliberate: a terminal child is never re-implemented (even
    with a stale lock file lying around), and a live holder outranks the
    status-based ``in-progress`` guess because it is a fact about a running
    process, not a lifecycle claim (SA-0MVQ5AXLF002PAQ3, AC3).

    Args:
        child: A child work-item dict from ``wl show --children``.

    Returns:
        The classification string.
    """
    status = str(child.get("status", ""))
    if _is_terminal_child(child):
        return "skip-terminal"
    if _probe_child_lock(str(child.get("id", ""))) is not None:
        return ACTION_SKIPPED_HELD
    if status in ("in-progress", "in_progress"):
        return "skip-in-progress"
    return "implement"


def _resolve_implementation_order(
    children: list[dict[str, Any]],
    blockers: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Return children in dependency order (blockers first).

    Builds a graph over the parent's children using outbound (depends-on)
    edges: for each child, the blockers that are THEMSELVES children of the
    parent form in-chain edges (blocker must be implemented before child).
    Blockers outside the children set (e.g. cross-project) do not create
    edges — they are reported as coordination notes by the caller.

    Ordering is a deterministic topological sort (Kahn's algorithm) with
    stable tie-breaks (``sortIndex``, then id). A dependency cycle fails
    fast: the function returns an empty list plus a clear error message
    naming the remaining (cyclic) children — no infinite recursion.

    Args:
        children: List of child work-item dicts from ``wl show --children``.
        blockers: Mapping child id → outbound dependency-edge dicts from
            ``wl dep list`` (may be empty).

    Returns:
        ``(ordered_children, None)`` on success, or
        ``([], error_message)`` when a cycle is detected.
    """
    child_by_id: dict[str, dict[str, Any]] = {
        str(c.get("id")): c for c in children if c.get("id")
    }
    child_ids = set(child_by_id)

    # in_degree[c] = number of in-chain blockers (blockers that are also
    # children of this parent). dependents[b] = children blocked by b.
    in_degree: dict[str, int] = {cid: 0 for cid in child_ids}
    dependents: dict[str, list[str]] = {cid: [] for cid in child_ids}
    for child in children:
        cid = str(child.get("id", ""))
        if cid not in child_ids:
            continue
        for edge in blockers.get(cid, []):
            bid = str(edge.get("id", ""))
            if bid in child_ids:
                in_degree[cid] += 1
                dependents[bid].append(cid)

    def _key(cid: str) -> tuple[int, str]:
        return (int(child_by_id[cid].get("sortIndex") or 0), cid)

    ready = sorted((cid for cid in child_ids if in_degree[cid] == 0), key=_key)
    ordered: list[str] = []
    while ready:
        cid = ready.pop(0)
        ordered.append(cid)
        for dep in dependents[cid]:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                ready.append(dep)
        ready.sort(key=_key)

    if len(ordered) != len(child_ids):
        remaining = sorted(child_ids - set(ordered), key=_key)
        cycle_path = " -> ".join(remaining)
        return [], (
            f"Dependency cycle detected among children: {cycle_path}. "
            f"Resolve the blocking relationships and re-run."
        )
    return [child_by_id[cid] for cid in ordered], None


def _next_child_to_implement(
    children: list[dict[str, Any]],
    blockers_map: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """Return the next child to run through the standard implement workflow.

    Children already terminal or in progress elsewhere are skipped
    (AC-4/AC-7). The *children* argument must already be in dependency
    order (see :func:`_resolve_implementation_order`). A child is startable
    only when every in-chain blocker (a blocker that is also one of these
    children) is already terminal — a blocked child is never implemented
    before its blockers (AC-2). Independent siblings of an in-progress
    child may still start.

    Args:
        children: List of child work-item dicts in dependency order.
        blockers_map: Mapping child id → outbound dependency-edge dicts from
            ``wl dep list`` (defaults to {}).

    Returns:
        The next child dict, or None when no child is startable (every
        remaining child is terminal, in progress elsewhere, or blocked by a
        non-terminal sibling).
    """
    blockers_map = blockers_map or {}
    child_ids = {str(c.get("id")) for c in children}
    terminal_ids = {
        str(c.get("id"))
        for c in children
        if _is_terminal_child(c)
    }
    for child in children:
        action = _classify_child(child)
        if action == "skip-terminal":
            continue
        if action == ACTION_SKIPPED_HELD:
            continue  # a live holder owns it; never a second session
        if action == "skip-in-progress":
            continue  # never clobber another agent's claim
        cid = str(child.get("id", ""))
        in_chain_blockers = [
            str(b.get("id"))
            for b in blockers_map.get(cid, [])
            if str(b.get("id")) in child_ids
        ]
        if all(bid in terminal_ids for bid in in_chain_blockers):
            return child
        # else: blocked by a non-terminal sibling — dependents come later
        # in the order and are transitively blocked; keep scanning for an
        # independent sibling that is startable.
    return None


# ---------------------------------------------------------------------------
# Git / Worktree helpers
# ---------------------------------------------------------------------------


def git_rev_parse_is_inside_work_tree() -> bool:
    """Check if current directory is inside a git worktree."""
    result = run_cmd(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def git_status(cwd: str | None = None) -> str:
    """Get the current git status (porcelain v1).

    Args:
        cwd: Working directory to check (defaults to current directory).

    Returns:
        Porcelain status output, or empty string on error.
    """
    result = run_cmd(
        ["git", "status", "--porcelain=v1", "-b"],
        cwd=cwd,
        capture=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


IMPLEMENT_SKILL_NAME = "implement"
#: ``extension.json`` key holding the project's expected-dirty path prefixes.
EXPECTED_DIRTY_KEY = "ignoreDirtyPaths"


def load_implement_extension(
    project_root: str | Path | None = None,
) -> SkillExtension | None:
    """Load the project-local ``implement`` skill extension.

    Discovery follows the shared convention
    (``<project_root>/.pi/skills_extensions/implement/``, contract
    SA-0MSQ7MQEJ0064ZB0): the project root defaults to the invoking git root,
    falling back to the cwd. Absence is a no-op and returns ``None``; a
    present-but-malformed ``extension.json`` raises
    :class:`SkillExtensionError` (the loader's loud-failure contract).
    """
    extension = load_extension(IMPLEMENT_SKILL_NAME, project_root)
    return extension if extension.present else None


def _parse_expected_dirty(extension: SkillExtension | None) -> tuple[str, ...]:
    """Parse the expected-dirty declaration from a loaded extension.

    The declaration is a list of repository-relative path prefixes under the
    ``ignoreDirtyPaths`` key. A trailing ``/`` is ignored so ``.llm-wiki/`` and
    ``.llm-wiki`` are equivalent. A non-string or empty entry is a project
    configuration error and raises :class:`SkillExtensionError` naming the
    file — a malformed declaration must never silently change the dirty-tree
    gate.
    """
    if extension is None:
        return ()
    raw = extension.data.get(EXPECTED_DIRTY_KEY)
    if raw is None:
        return ()
    if isinstance(raw, str):
        entries: list[Any] = [raw]
    elif isinstance(raw, (list, tuple)):
        entries = list(raw)
    else:
        raise SkillExtensionError(
            f"Invalid '{EXPECTED_DIRTY_KEY}' in {extension.data_path}: "
            "expected a string or a list of strings."
        )
    paths: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            raise SkillExtensionError(
                f"Invalid '{EXPECTED_DIRTY_KEY}' entry {entry!r} in "
                f"{extension.data_path}: every entry must be a non-empty string."
            )
        paths.append(entry.strip())
    return tuple(paths)


def expected_dirty_paths(
    project_root: str | Path | None = None,
) -> tuple[str, ...]:
    """Return the project-declared expected-dirty path prefixes.

    Loads (and validates) the implement extension for *project_root*. Returns
    an empty tuple when no extension — or no ``ignoreDirtyPaths`` declaration —
    is present, so an absent extension is a no-op.
    """
    return _parse_expected_dirty(load_implement_extension(project_root))


def _path_is_expected_dirty(file_path: str, expected_dirty: tuple[str, ...]) -> bool:
    """True when *file_path* matches a declared expected-dirty prefix."""
    for prefix in expected_dirty:
        normalised = prefix.rstrip("/")
        if not normalised:
            continue
        if file_path == normalised or file_path.startswith(normalised + "/"):
            return True
    return False


def git_has_dirty_files(
    status_output: str | None = None,
    expected_dirty: tuple[str, ...] = (),
) -> bool:
    """Check if there are uncommitted changes outside .worklog/.

    Args:
        status_output: Optional pre-fetched git status output.
        expected_dirty: Project-declared path prefixes that are expected to be
            dirty and therefore do not count (e.g. ``.llm-wiki/``). Empty by
            default, so callers that do not opt in keep the original
            behaviour byte-for-byte.

    Returns:
        True if any non-.worklog, non-expected-dirty file is dirty.
    """
    if status_output is None:
        status_output = git_status()
    for line in status_output.splitlines():
        # Skip branch info line
        if line.startswith("##"):
            continue
        # Skip .worklog/ changes
        file_path = line[3:].strip() if len(line) > 3 else ""
        if file_path.startswith(".worklog/"):
            continue
        # Skip project-declared expected-dirty paths (never a hard-fail).
        if _path_is_expected_dirty(file_path, expected_dirty):
            continue
        if line.strip():
            return True
    return False


# ---------------------------------------------------------------------------
# Dirty-tree relevance helpers (SA-0MV0NSOGE000OSMR)
# ---------------------------------------------------------------------------


def dirty_paths_from_status(
    status_output: str,
    expected_dirty: tuple[str, ...] = (),
) -> list[str]:
    """Return the dirty paths from porcelain status, excluding ``.worklog/``.

    Mirrors the filtering used by :func:`git_has_dirty_files` so the paths
    reported to the operator are exactly the ones that caused the gate to
    trip. Renames are reduced to their destination path and project-declared
    expected-dirty prefixes are ignored.
    """
    paths: list[str] = []
    for line in status_output.splitlines():
        if line.startswith("##") or not line.strip():
            continue
        file_path = line[3:].strip() if len(line) > 3 else ""
        if " -> " in file_path:
            file_path = file_path.split(" -> ", 1)[1].strip()
        if not file_path or file_path.startswith(".worklog/"):
            continue
        if _path_is_expected_dirty(file_path, expected_dirty):
            continue
        paths.append(file_path)
    return paths


# Matches ``## Key Files``, ``## Key Files (predicted)`` and the plan
# helper's ``**Key Files:**`` form (SA-0MV0NSOGE000OSMR).
_KEY_FILES_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s*Key Files.*|\*\*Key Files:?\*\*.*)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def extract_key_files_from_description(description: str) -> list[str]:
    """Extract file paths from a work item description's Key Files section.

    Handles the heading variants seen across real work items — ``## Key
    Files``, ``## Key Files (predicted)`` and the plan helper's
    ``**Key Files:**`` form. Returns an empty list when no Key Files section
    is present; callers treat that as "relevance cannot be judged" and stay
    conservative (SA-0MV0NSOGE000OSMR).
    """
    if not description:
        return []
    match = _KEY_FILES_HEADING_RE.search(description)
    if not match:
        return []
    files: list[str] = []
    for line in description[match.end():].splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # A further markdown heading, or a standalone bold section marker,
        # ends the Key Files list.
        if re.match(r"^#{1,6}\s", stripped) or (
            stripped.startswith("**") and not stripped.startswith("- ")
        ):
            break
        backtick = re.search(r"`([^`]+)`", stripped)
        if backtick:
            candidate = backtick.group(1).strip()
        else:
            bullet = re.match(r"^[-*]\s+(\S+)", stripped)
            candidate = bullet.group(1) if bullet else ""
        if candidate:
            files.append(candidate)
    return files


def dirty_file_matches_key_files(dirty_path: str, key_files: list[str]) -> bool:
    """True when *dirty_path* plausibly overlaps one of *key_files*.

    Matching is deliberately generous (errs toward *relevant*, which keeps
    the conservative abort) — equality, directory-prefix containment in
    either direction, or a matching basename. This is agent-discretion
    encoded as a simple, deterministic overlap check; it is not a whitelist
    (SA-0MV0NSOGE000OSMR).
    """
    dirty = dirty_path.strip().lstrip("./")
    if not dirty:
        return False
    dirty_base = os.path.basename(dirty.rstrip("/"))
    for key in key_files:
        candidate = key.strip().lstrip("./")
        if not candidate:
            continue
        if dirty == candidate:
            return True
        if dirty.startswith(candidate.rstrip("/") + "/"):
            return True
        if candidate.startswith(dirty.rstrip("/") + "/"):
            return True
        if dirty_base and os.path.basename(candidate.rstrip("/")) == dirty_base:
            return True
    return False


def classify_dirty_tree_relevance(
    status_output: str,
    description: str,
    expected_dirty: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Decide whether a dirty main checkout can be safely left behind.

    The start phase may proceed past the dirty-tree safety gate only when the
    work item's description exposes a Key Files section *and* none of the
    dirty files overlap those paths. When there is no Key Files section the
    relevance of the dirty files cannot be judged, so the caller keeps the
    conservative abort-and-ask behaviour (SA-0MV0NSOGE000OSMR).

    Returns a dict with keys:
        ``dirty`` (bool), ``dirty_files`` (list), ``key_files`` (list),
        ``relevant_files`` (list), ``can_proceed`` (bool) and ``reason``
        (human-readable explanation).
    """
    dirty_files = dirty_paths_from_status(status_output, expected_dirty)
    key_files = extract_key_files_from_description(description)
    relevant = [
        path for path in dirty_files
        if dirty_file_matches_key_files(path, key_files)
    ]

    if not dirty_files:
        return {
            "dirty": False,
            "dirty_files": [],
            "key_files": key_files,
            "relevant_files": [],
            "can_proceed": True,
            "reason": "clean working tree",
        }
    if not key_files:
        return {
            "dirty": True,
            "dirty_files": dirty_files,
            "key_files": [],
            "relevant_files": [],
            "can_proceed": False,
            "reason": (
                "no Key Files section in the work item description — "
                "cannot judge dirty-file relevance"
            ),
        }
    if relevant:
        return {
            "dirty": True,
            "dirty_files": dirty_files,
            "key_files": key_files,
            "relevant_files": relevant,
            "can_proceed": False,
            "reason": (
                "dirty file(s) overlap the work item's Key Files: "
                + ", ".join(relevant)
            ),
        }
    return {
        "dirty": True,
        "dirty_files": dirty_files,
        "key_files": key_files,
        "relevant_files": [],
        "can_proceed": True,
        "reason": (
            "dirty files do not overlap the work item's Key Files — "
            "left untouched in the main checkout"
        ),
    }


def _current_checkout_is_linked_worktree(cwd: str | None = None) -> bool:
    """True when the current working tree is a linked git worktree.

    A linked worktree has ``.git`` as a file (a gitdir pointer) rather than a
    directory. The dirty-tree safety gate protects the *main checkout* — the
    dirty files it leaves behind when forking a worktree — so it is skipped
    when ``phase_start`` is invoked from inside a worktree (e.g. resuming a
    driven child), where any dirty files are the agent's own in-progress work
    (SA-0MV0NSOGE000OSMR).
    """
    result = run_cmd(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        check=False,
        timeout=30,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return False
    return _is_worktree(Path(result.stdout.strip()))


def _surface_extension_prose(
    extension: SkillExtension | None,
    which: str,
    json_output: bool,
    report: dict[str, Any],
) -> str | None:
    """Surface a project-local prose hook and record it in the report.

    Called with ``which="prefix"`` before the skill's first actionable step
    and ``which="postfix"`` after its final step. Returns the prose text (or
    ``None`` when absent) and prints a delimited human-readable block when not
    emitting JSON. The JSON report always carries the text so a driving agent
    can consume it either way.
    """
    if extension is None:
        return None
    text = extension.prefix if which == "prefix" else extension.postfix
    if not text:
        return None
    report.setdefault("extension", {})[f"{which}_prose"] = text
    if not json_output:
        print()
        print("=" * 60)
        print(f"  Project extension ({which})")
        print("=" * 60)
        print(text.rstrip("\n"))
        print("=" * 60)
        print()
    return text


def _surface_postfix_prose(
    project_root: str | Path | None,
    json_output: bool,
    report: dict[str, Any],
) -> str | None:
    """Load the extension and surface its postfix hook (best-effort).

    A malformed extension is logged and never fails an already-successful
    finish.
    """
    try:
        extension = load_implement_extension(project_root)
    except SkillExtensionError as exc:  # pragma: no cover - defensive
        LOG.warning("Could not load implement extension for postfix prose: %s", exc)
        return None
    return _surface_extension_prose(extension, "postfix", json_output, report)


def _main_checkout_offending_paths(
    repo_root: str, expected_dirty: tuple[str, ...] = ()
) -> list[str]:
    """Return the non-``.worklog/`` dirty paths in a checkout.

    Used by the implementation-placement gates to name the offending files
    when work landed in the main checkout instead of a worktree.

    Args:
        repo_root: Absolute path to the main checkout root.
        expected_dirty: Project-declared path prefixes to exclude.

    Returns:
        Sorted, de-duplicated list of paths (relative to *repo_root*) that are
        dirty outside ``.worklog/`` and not expected-dirty. Empty when the
        checkout is clean or only expected-dirty paths are modified.
    """
    status_output = git_status(cwd=repo_root)
    paths: list[str] = []
    for line in status_output.splitlines():
        if line.startswith("##") or not line.strip():
            continue
        # Porcelain v1: ``XY <path>`` — the path starts at column 4. Renames
        # are rendered as ``XY <old> -> <new>``; report the destination.
        file_path = line[3:].strip() if len(line) > 3 else ""
        if " -> " in file_path:
            file_path = file_path.split(" -> ", 1)[1].strip()
        if not file_path or file_path.startswith(".worklog/"):
            continue
        if _path_is_expected_dirty(file_path, expected_dirty):
            continue
        paths.append(file_path)
    return sorted(set(paths))


def _main_checkout_status_map(repo_root: str) -> dict[str, str]:
    """Parse ``git status --porcelain=v1`` into a ``{path: code}`` map.

    Only the working-tree/status surface is captured (no refs, config or
    worktree registrations), so finish's own fast-forward, ref movement and
    agent-worktree register/remove do not move the map. ``.worklog/``
    bookkeeping is filtered out here — the finish run itself writes there
    via ``wl`` (comments, status updates, cache).

    Args:
        repo_root: Absolute path to the main checkout.

    Returns:
        A mapping of relative path → porcelain XY code, excluding
        ``.worklog/`` paths.
    """
    status_output = git_status(cwd=repo_root)
    entries: dict[str, str] = {}
    for line in status_output.splitlines():
        if line.startswith("##") or not line.strip():
            continue
        if len(line) < 4:
            continue
        code = line[:2]
        file_path = line[3:].strip()
        if " -> " in file_path:
            file_path = file_path.split(" -> ", 1)[1].strip()
        if not file_path or file_path.startswith(".worklog/"):
            continue
        entries[file_path] = code
    return entries


def _snapshot_main_checkout(repo_root: str) -> dict[str, Any]:
    """Snapshot the main checkout's status surface at a point in time.

    The finish main-checkout guard (WL-0MVPADZ1Z000FOSG AC3) compares two of
    these snapshots to detect foreign tracked-file mutations of the main
    checkout during a worktree-scoped finish run.

    Args:
        repo_root: Absolute path to the main checkout.

    Returns:
        ``{"taken_at": <ISO timestamp>, "status": {path: code}}``.
    """
    return {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "status": _main_checkout_status_map(repo_root),
    }


def _main_checkout_guard_violations(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    own_churn: set[str] | frozenset[str] | tuple[str, ...] = (),
    expected_dirty: tuple[str, ...] = (),
) -> list[str]:
    """Return the offending main-checkout paths between two snapshots.

    A path is offending when its status entry *changed* between snapshots
    (added, deleted or modified) and it is none of:

    - under ``.worklog/`` (the finish run's own bookkeeping surface),
    - a project-declared expected-dirty path (*expected_dirty*), nor
    - a path in *own_churn* whose status code is not a deletion.

    *own_churn* is the file list of finish's own Step-5 fast-forward of the
    main checkout's ``dev`` branch: those working-tree changes are attributed
    to finish and never abort. It is deliberately narrow
    (SA-0MVXVC03O0034A7V): :func:`_record_finish_own_churn` only ever returns
    checkout-relative paths that were *observed dirty in this checkout* after
    the advance, so a path belonging to another checkout or repository can
    never be allow-listed here. A foreign spelling of a dirty path (absolute,
    or a ``..`` escape) simply does not compare equal to the normalised
    checkout-relative status path, so it never satisfies the allow-list and
    the real path stays offending. A deletion (``D`` in the code) is **never**
    allow-listed either, even when the path appears in *own_churn* — a
    fast-forward cannot delete a file relative to the tracked tree, so a ``D``
    entry is foreign by construction (WL-0MVPADZ1Z000FOSG AC3).

    The operator-facing contract lives in
    ``docs/dev/implement-skill-reference.md`` (§ Finish main-checkout guard →
    Attribution contract (`own_churn`)).

    Args:
        baseline: The :func:`_snapshot_main_checkout` taken at run start.
        current: The :func:`_snapshot_main_checkout` taken at a verify point.
        own_churn: Paths changed by finish's own fast-forward.
        expected_dirty: Project-declared dirty path prefixes to exclude.

    Returns:
        Sorted, de-duplicated list of ``"<path> [<code>]"`` entries.
    """
    before = baseline.get("status", {})
    after = current.get("status", {})
    own = set(own_churn or ())
    offending: list[str] = []
    for path, code in sorted(after.items()):
        if path.startswith(".worklog/") or _path_is_expected_dirty(
            path, expected_dirty
        ):
            continue
        if path in own and "D" not in code:
            continue  # finish's own fast-forward churn
        if path not in before or before[path] != code:
            offending.append(f"{path} [{code}]")
    return offending


def _repository_root_mismatch(repo_root: str) -> str:
    """Explain why *repo_root* is not a main working-tree root ("" when it is).

    Attribution helpers must never treat a path that merely *contains* a
    repository as that repository: a subdirectory or a linked worktree's root
    still answers ``git diff --name-only`` (relative to the repository), yet
    the finish run does not own the files it names. The check therefore
    requires

    * ``git rev-parse --show-toplevel`` for *repo_root* to be *repo_root*
      itself, and
    * ``git rev-parse --git-common-dir`` (whose parent is the main working
      tree root) to be inside *repo_root* — i.e. *repo_root* is the main
      checkout, not a linked worktree of one.

    Args:
        repo_root: Candidate main-checkout root.

    Returns:
        A human-readable reason, or ``""`` when *repo_root* is the root of a
        non-linked working tree.
    """
    try:
        root = Path(repo_root).resolve()
    except OSError:
        return f"{repo_root} cannot be resolved"
    if not root.is_dir():
        return f"{repo_root} is not a directory"
    toplevel_result = run_cmd(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo_root,
        check=False,
    )
    if toplevel_result.returncode != 0 or not toplevel_result.stdout.strip():
        return f"{repo_root} is not inside a git working tree"
    toplevel = Path(toplevel_result.stdout.strip()).resolve()
    if toplevel != root:
        return (
            f"{repo_root} is not the repository root "
            f"(git --show-toplevel is {toplevel})"
        )
    common_result = run_cmd(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo_root,
        check=False,
    )
    if common_result.returncode != 0 or not common_result.stdout.strip():
        return f"git --git-common-dir failed in {repo_root}"
    common_dir = Path(common_result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = toplevel / common_dir
    common_dir = common_dir.resolve()
    main_root = common_dir.parent
    if main_root != root:
        return (
            f"{repo_root} is a linked worktree, not its main checkout "
            f"(git --git-common-dir is {common_dir}, whose parent is "
            f"{main_root})"
        )
    return ""


def _record_finish_own_churn(
    repo_root: str,
    before_head: str,
    after_head: str,
    *,
    observed: dict[str, str] | None = None,
    not_observed: list[str] | None = None,
) -> list[str]:
    """Paths of finish's own fast-forward that were *observed* dirty here.

    The diff between the main checkout's HEAD before the Step-5 fast-forward
    and after is **not** by itself churn: on 2026-10-31 a finish reported seven
    paths (``docs/Game Design Document.md``, five ``src/powerups/*``, …) whose
    ``baseline_paths`` was 0 — that was the file list of the commit local
    ``dev`` was advanced to, i.e. other work items' work, not anything this
    run touched in this checkout (SA-0MVXVC03O0034A7V).

    So attribution is constrained three ways:

    1. *repo_root* must be the root of the finished working tree **and** its
       main checkout — a subdirectory or a linked worktree is refused loudly
       (:func:`_repository_root_mismatch`), because the diff would then name
       paths this run does not own;
    2. every diff path must be checkout-relative (absolute and ``..``-escaping
       entries are dropped) **and** resolve inside *repo_root*;
    3. every surviving path must be *observed dirty in the finished checkout*
       after the advance — the post-fast-forward status map, re-snapshotted
       here unless supplied via *observed*.

    Dropped paths are reported through *not_observed* so an operator sees what
    the diff claimed and could not be attributed; they are a diagnostic only
    and are **never** allow-listed by the main-checkout guard. A deletion is
    likewise never allow-listed (the guard keeps that exclusion). The
    operator-facing contract is
    ``docs/dev/implement-skill-reference.md`` § Finish main-checkout guard →
    Attribution contract (`own_churn`).

    Args:
        repo_root: Absolute path to the main checkout being finished.
        before_head: ``git rev-parse HEAD`` before the fast-forward.
        after_head: The fast-forwarded HEAD (commit hash just merged).
        observed: Optional status map to intersect against; re-snapshotted from
            *repo_root* when omitted.
        not_observed: Optional list extended with a ``"<path> [<reason>]"``
            entry for every dropped path.

    Returns:
        Sorted file list of attributable paths, or ``[]`` when the roots or
        refs are unavailable or nothing was observed dirty.
    """
    if not before_head or not after_head:
        return []

    mismatch = _repository_root_mismatch(repo_root)
    if mismatch:
        LOG.warning(
            "Refusing to attribute finish's own fast-forward churn: %s. No "
            "path is allow-listed; the main-checkout guard stays strict.",
            mismatch,
        )
        return []

    result = run_cmd(
        ["git", "diff", "--name-only", before_head, after_head],
        cwd=repo_root,
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return []

    root = Path(repo_root).resolve()
    if observed is None:
        observed = _main_checkout_status_map(repo_root)
    own: list[str] = []
    dropped: list[str] = []
    for raw in result.stdout.splitlines():
        path = raw.strip()
        if not path:
            continue
        candidate = Path(path)
        reason = ""
        if candidate.is_absolute():
            reason = "absolute path"
        elif ".." in candidate.parts:
            reason = "path contains '..'"
        else:
            resolved = (root / candidate).resolve()
            if resolved != root and root not in resolved.parents:
                reason = f"resolves outside {root}"
        if not reason and path not in observed:
            reason = "not observed dirty in the finished checkout"
        if reason:
            dropped.append(f"{path} [{reason}]")
            continue
        own.append(path)

    if not_observed is not None:
        not_observed.extend(dropped)
    return sorted(set(own))


def _main_checkout_actor_evidence(
    repo_root: str, worktree_path: str
) -> str:
    """Best-effort concurrent-actor evidence for the guard's abort message.

    Sources, in order:

    1. other worktree state files (``.implement_state.json``) under the main
       checkout — each names its owning work item and start time,
    2. live processes whose ``/proc/<pid>/cwd`` resolves to the main
       checkout root (Linux only; skipped elsewhere).

    Returns ``"unknown"`` when no source yields evidence (the abort message
    still fires — evidence is informative, never gating).

    Args:
        repo_root: Absolute path to the main checkout.
        worktree_path: This finish run's own worktree (excluded from the
            evidence scan).
    """
    own = str(Path(worktree_path).resolve())
    evidence: list[str] = []
    worktrees_dir = Path(repo_root) / ".worklog" / "worktrees"
    try:
        state_files = sorted(worktrees_dir.glob("wl-*/.implement_state.json"))
    except OSError:
        state_files = []
    for state_file in state_files:
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(state_file.parent.resolve()) == own:
            continue
        evidence.append(
            f"work item {data.get('work_item_id', '?')} "
            f"(worktree {state_file.parent}, started "
            f"{data.get('started_at', '?')})"
        )
        if len(evidence) >= 5:
            break
    try:
        root = str(Path(repo_root).resolve())
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                cwd = os.readlink(f"/proc/{entry}/cwd")
            except OSError:
                continue
            if str(Path(cwd).resolve()) == root:
                evidence.append(f"pid {entry} with cwd in the main checkout")
                if len(evidence) >= 8:
                    break
    except OSError:
        pass
    return "; ".join(evidence) if evidence else "unknown"


def _main_checkout_guard_message(
    repo_root: str,
    violations: list[str],
    actor_evidence: str,
    stage: str,
) -> str:
    """Render the loud-abort message for a main-checkout guard failure.

    Names every offending path, the concurrent-actor evidence available at
    runtime and the recovery command, so an agent hitting the abort can
    restore the main checkout and re-run finish without spelunking.
    """
    listing = "\n".join(f"  - {p}" for p in violations[:20])
    if len(violations) > 20:
        listing += f"\n  … and {len(violations) - 20} more"
    return (
        f"Main-checkout mutation detected ({stage} verify): tracked-file "
        f"changes in the main checkout at {repo_root} were NOT made by this "
        f"finish run. Aborting instead of silently committing, fast-forwarding "
        f"or pushing them.\n"
        f"Offending paths:\n{listing}\n"
        f"Concurrent actor evidence: {actor_evidence}\n"
        f"Recovery: in the main checkout run `git checkout -- <path>` (or "
        f"`git restore <path>`) for each path above to restore the tracked "
        f"files from HEAD, then re-run `implement.py finish`."
    )


#: Every stage label the finish main-checkout guard can record in
#: ``report["steps"]["main_checkout_guard"]["verifies"]``
#: (SA-0MVXVBYXI000QMXM). ``pre_push`` and ``pre_worktree_removal`` are the
#: push-path verifies; the failure-path stages are recorded when finish aborts
#: before the push; ``refactor`` is advisory/report-only (it never aborts).
#: Documented for operators in ``docs/dev/implement-skill-reference.md``
#: (§ Finish main-checkout guard → Verify stages);
#: ``skill/implement/tests/test_guard_docs.py`` fails when this tuple and the
#: documented table drift apart, so add a stage in both places.
VERIFY_STAGES: tuple[str, ...] = (
    "refactor",
    "build_failure",
    "gate_failure",
    "user_abort",
    "pre_push",
    "pre_worktree_removal",
)


def _verify_main_checkout(
    guard: dict[str, Any],
    baseline: dict[str, Any],
    *,
    repo_root: str,
    worktree_path: str,
    own_churn: list[str],
    expected_dirty: tuple[str, ...],
    stage: str,
) -> list[str]:
    """Re-snapshot the main checkout and record a guard verify entry.

    Appends ``{"stage", "at", "verdict", "offending"?}`` to
    ``guard["verifies"]``, updates ``guard["verdict"]`` and returns the
    offending paths (empty list = pass), so an operator can read the guard's
    work straight from the finish report.

    Args:
        guard: The ``report["steps"]["main_checkout_guard"]`` dict.
        baseline: Snapshot taken at run start.
        repo_root: Absolute path to the main checkout.
        worktree_path: This finish run's own worktree.
        own_churn: Paths changed by finish's own fast-forward.
        expected_dirty: Project-declared dirty path prefixes to exclude.
        stage: Verify stage label — one of :data:`VERIFY_STAGES`.

    Returns:
        The offending paths (empty list on pass).
    """
    current = _snapshot_main_checkout(repo_root)
    violations = _main_checkout_guard_violations(
        baseline,
        current,
        own_churn=own_churn,
        expected_dirty=expected_dirty,
    )
    entry: dict[str, Any] = {
        "stage": stage,
        "at": current["taken_at"],
        "verdict": "fail" if violations else "pass",
    }
    if violations:
        entry["offending"] = violations
        entry["actor_evidence"] = _main_checkout_actor_evidence(
            repo_root, worktree_path
        )
    guard["verifies"].append(entry)
    guard["verdict"] = entry["verdict"]
    return violations


# ---------------------------------------------------------------------------
# Per-checkout finish lock (WL-0MVPADZ1Z000FOSG AC5)
# ---------------------------------------------------------------------------

#: Lock-file path relative to the main checkout root. One exclusive advisory
#: ``flock`` holder per repository — two finishes on the same main checkout
#: can never overlap (2026-10-26 incident: two concurrent finishes plus a
#: full-scope test run on one shared checkout). The mechanics live in the
#: shared :mod:`shared.repo_lock` helper (SA-0MVPZG3IQ009IJMO) so the same
#: lock also guards the adjacent parent-branch refresh window in
#: :func:`phase_start`.
MAIN_CHECKOUT_LOCK_RELPATH = _REPO_LOCK_RELPATH

#: Bounded wait for the per-repository lock in ``finish`` (seconds). Default
#: ``0`` = fail fast: a second finish refuses immediately with an actionable
#: message naming the holder (AC3 allows fail-fast). Set to a positive value
#: (env ``IMPLEMENT_FINISH_LOCK_TIMEOUT``) to wait that long for a concurrent
#: finish instead — the wait is always bounded, never indefinite.
DEFAULT_FINISH_LOCK_TIMEOUT = 0.0

#: Bounded wait for the parent-branch refresh window in ``start`` (seconds).
#: ``start`` must never be blocked by a concurrent finish: after this bound the
#: refresh is skipped (fail-open) and the local branch is used as the base.
DEFAULT_START_LOCK_TIMEOUT = 30.0

ENV_FINISH_LOCK_TIMEOUT = "IMPLEMENT_FINISH_LOCK_TIMEOUT"
ENV_START_LOCK_TIMEOUT = "IMPLEMENT_START_LOCK_TIMEOUT"

#: Override for the per-work-item child-lock store (test isolation).
#: Unset in production; the worklog store resolved from the item id is used.
#: ``phase_start`` acquires a real ``flock`` before its mocked steps, so an
#: un-isolated suite would race real lock files for synthetic test ids
#: (SA-0MVWJ6U3J003TLRZ). Values are read per call, so a fixture that sets it
#: also isolates tests that load this module by file path.
ENV_CHILD_LOCK_DIR = "IMPLEMENT_CHILD_LOCK_DIR"


def _resolve_lock_timeout(env_var: str, default: float) -> float:
    """Resolve a bounded lock wait from the environment (fail-safe default)."""
    raw = os.environ.get(env_var)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        LOG.warning(
            "Ignoring invalid %s=%r (expected seconds); using %gs",
            env_var, raw, default,
        )
        return default
    if value < 0:
        LOG.warning(
            "Ignoring negative %s=%r; using %gs", env_var, raw, default,
        )
        return default
    return value


def _finish_lock_timeout() -> float:
    """Bounded wait used by ``phase_finish`` for the main-checkout lock."""
    return _resolve_lock_timeout(ENV_FINISH_LOCK_TIMEOUT, DEFAULT_FINISH_LOCK_TIMEOUT)


def _start_lock_timeout() -> float:
    """Bounded wait used by ``phase_start`` for the parent-branch refresh."""
    return _resolve_lock_timeout(ENV_START_LOCK_TIMEOUT, DEFAULT_START_LOCK_TIMEOUT)


def _main_checkout_lock_path(repo_root: str) -> Path:
    """Absolute path of the main-checkout finish lock file."""
    return RepoLock(repo_root, relpath=MAIN_CHECKOUT_LOCK_RELPATH).path


def _release_main_checkout_lock(fd: int | None) -> None:
    """Release the held main-checkout lock. Idempotent (None/no-op)."""
    release_repo_lock(fd)


def acquire_main_checkout_lock(
    repo_root: str,
    work_item_id: str,
    timeout: float = 0.0,
) -> tuple[int | None, str | None]:
    """Take the exclusive per-checkout lock (fail fast by default).

    Thin ``implement.py`` wrapper over :class:`shared.repo_lock.RepoLock`. The
    lock is an advisory ``flock`` on
    ``<main-checkout>/.worklog/locks/main-checkout.lock``, released by the
    kernel when the holder dies (SIGKILL included) — no stale-lock bookkeeping
    exists to trust (WL-0MVPADZ1Z000FOSG AC4/AC5). Holder metadata is written
    into the lock file after acquisition for the refusal message only; it never
    decides ownership and a metadata write failure never fails the acquisition.

    Args:
        repo_root: Absolute path to the main checkout.
        work_item_id: The work item this run belongs to (recorded in the
            refusal message as the requester).
        timeout: Bounded wait in seconds (``0`` = fail fast). A positive value
            waits up to that deadline, then refuses with an actionable
            timeout message — never an indefinite hang.

    Returns:
        ``(fd, None)`` on success (caller must release via
        :func:`_release_main_checkout_lock`), or ``(None, message)`` with a
        complete, actionable refusal message when the lock is held.
    """
    return RepoLock(repo_root, relpath=MAIN_CHECKOUT_LOCK_RELPATH).acquire(
        work_item_id, timeout=timeout
    )


def git_worktree_add(
    branch: str, worktree_path: str, parent_branch: str = DEFAULT_PARENT_BRANCH
) -> bool:
    """Create a new git worktree from a parent branch.

    Args:
        branch: Name for the new branch.
        worktree_path: Path for the worktree directory.
        parent_branch: Source branch to fork from.

    Returns:
        True if the worktree was created.
    """
    result = run_cmd(
        ["git", "worktree", "add", "--track", "-b", branch, worktree_path, parent_branch],
        check=False,
        timeout=120,
    )
    return result.returncode == 0


# A parent branch whose ``remote`` key must never change: a worktree
# registration writes ``.`` into ``branch.<new-wl-branch>.remote`` only; the
# parent's key is guarded by :func:`assert_parent_tracking_untouched`.


def _git_config_get(cwd: str, key: str) -> str | None:
    """Read a single ``git config --get`` value, or ``None`` when unset."""
    result = run_cmd(
        ["git", "config", "--get", key],
        cwd=cwd,
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def warn_self_referential_upstream(repo_root: str) -> dict[str, Any]:
    """Fail-open warning: long-lived branches whose upstream is themselves.

    ``branch.<name>.remote = "."`` on ``dev``/``main`` makes a plain
    ``git pull`` a silent no-op (SA-0MVX1M6HH004YPX2): the machine keeps
    running stale skills and stale worklog data while every tool reports
    "up to date".

    ``implement.py start`` surfaces that as a **warning only**
    (SA-0MVX35AUC003CNUD AC2/AC3): a work item is never blocked and no config
    is ever rewritten — detection is read-only, and repair stays an explicit
    operator action (``git branch --set-upstream-to=origin/<branch> <branch>``
    or the detection module's ``--fix``). A missing detection module, a
    non-repository target or a git failure degrades to a skip.

    Args:
        repo_root: The main checkout root to inspect.

    Returns:
        A verdict mapping with ``status`` (``ok``/``warning``/``skipped``),
        plus ``defect_count`` and ``defects`` when defects were found.
    """
    if _detect_self_referential_upstreams is None:
        LOG.warning(
            "Self-referential-upstream check skipped: detection module not "
            "available"
        )
        return {"status": "skipped", "reason": "detection module not available"}
    try:
        # Worktree branches are reported as ``expected``; only ``defect``
        # findings are the machine-wide breakage worth warning about.
        defects = [
            finding
            for finding in _detect_self_referential_upstreams(repo_root)
            if finding.get("type") == "defect"
        ]
    except Exception as exc:  # noqa: BLE001 -- fail-open; see the comment below
        # Broad by design (BLE001): the detector can raise not only
        # ``NotARepositoryError`` but also a ``TypeError`` (a non-path repo),
        # an ``OSError`` from the git subprocess or a broken-config failure.
        # Every one of them must degrade to the logged skip below and never
        # block a work item (SA-0MVX35AUC003CNUD AC2/AC3). Pinned by
        # skill/implement/tests/test_self_upstream_wiring.py.
        LOG.warning("Self-referential-upstream check skipped: %s", exc)
        return {"status": "skipped", "reason": str(exc)}
    if not defects:
        return {"status": "ok", "defect_count": 0}
    branches = ", ".join(str(defect.get("branch")) for defect in defects)
    LOG.warning(
        "Long-lived branch(es) track themselves (%s): `git pull` fetches from "
        "this repository and silently changes nothing. Repair explicitly: "
        "git branch --set-upstream-to=origin/<branch> <branch> (or "
        "python3 skill/shared/self_referential_upstream.py --fix).",
        branches,
    )
    return {
        "status": "warning",
        "defect_count": len(defects),
        "defects": defects,
    }


def assert_parent_tracking_untouched(
    repo_root: str,
    parent_branch: str,
    before: tuple[str | None, str | None],
) -> None:
    """Fail loudly when a worktree registration poisoned the parent's upstream.

    ``git worktree add --track -b wl-* <path> <parent>`` writes
    ``branch.<wl-*>.remote = "."`` for the new branch and leaves the parent's
    key alone — the *designed* shape (SA-0MVX1M6HH004YPX2, pinned by
    ``skill/shared/tests/test_self_referential_upstream.py``).
    ``branch.<name>.remote`` is one key per **branch name** in the shared
    config, so if the registration ever wrote ``.`` onto the parent branch
    name, every ``git pull`` on that machine would silently stop fetching
    from ``origin``.

    This guard runs immediately after a fresh worktree registration and fails
    closed: a parent that now tracks ``.`` while it did not before is a hard
    error, not a warning, because the write path is the framework's own
    (SA-0MVX35ABT000I1PD AC1).

    Args:
        repo_root: Repository root whose config to read (the common config
            every worktree shares).
        parent_branch: The long-lived parent branch name that must stay
            unaffected.
        before: The ``(remote, merge)`` tracking pair captured *before* the
            registration. None-values are indistinguishable from "not set".

    Raises:
        RuntimeError: when *parent_branch* now has ``remote = "."`` while it
            did not before — i.e. this registration wrote the self-upstream.
    """
    after_remote = _git_config_get(repo_root, f"branch.{parent_branch}.remote")
    after_merge = _git_config_get(repo_root, f"branch.{parent_branch}.merge")
    before_remote = before[0]
    before_merge = before[1]
    if after_remote == "." and before_remote != ".":
        raise RuntimeError(
            f"Worktree registration wrote a self-referential upstream onto "
            f"parent branch {parent_branch!r}: branch.{parent_branch}.remote "
            f"changed from {before_remote!r} to '.' (merge="
            f"{after_merge!r}, was {before_merge!r}). This makes `git pull` on "
            f"{parent_branch} a silent no-op (SA-0MVX1M6HH004YPX2). Refusing "
            "to continue; the defect must be repaired with "
            f"`git branch --set-upstream-to=origin/{parent_branch} "
            f"{parent_branch}` before any worktree is created."
        )


# ---------------------------------------------------------------------------
# Temp-index staging: test the tree the commit will produce
# (SA-0MVQHVGFR006W8LG, F6 of SA-0MVL2NH1G0094FA2)
# ---------------------------------------------------------------------------

#: Temporary finish-gate index names. The prefix is also the marker the abort
#: contract's artefact detector looks for, so it is part of the contract.
GATE_INDEX_PREFIX = "implement-gate-"
GATE_INDEX_SUFFIX = ".index"

#: The temporary index currently exported to a running suite, or None. Kept
#: module-level so the signal handler can remove it on SIGTERM/SIGINT.
_gate_index_global: str | None = None


def _remove_gate_index(index_path: str | None) -> None:
    """Best-effort removal of a temporary finish-gate index (and its lock).

    Shared by the staging context manager's ``finally`` and the signal
    handler, so no exit path can leave an index-shaped artefact in the git
    directory (the F5 abort contract, SA-0MVQHVCRE0070YO6).
    """
    if not index_path:
        return
    for candidate in (index_path, f"{index_path}.lock"):
        try:
            os.unlink(candidate)
        except FileNotFoundError:
            continue
        except OSError as exc:
            LOG.warning(
                "Could not remove the finish gate's temporary index %s: %s",
                candidate,
                exc,
            )


def _run_git_plumbing(
    cwd: str,
    args: list[str],
    *,
    index_file: str | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Run ``git <args>`` in *cwd* against an explicitly chosen index.

    The finish gate's temp-index plumbing must act on the real worktree and on
    a *specific* index file, never on whatever ``GIT_INDEX_FILE`` happens to be
    exported into this process (the gate exports it while the suite runs).
    ``index_file`` is therefore authoritative: a path is exported to the child,
    and ``None`` **removes** any ambient value so the real index is used.

    Deliberately not routed through :func:`run_cmd`: these are fast, local
    plumbing calls that must reach the real repository even when a caller (or a
    test harness) stubs the surrounding git boundary.
    """
    env = dict(os.environ)
    env.pop("GIT_INDEX_FILE", None)
    if index_file is not None:
        env["GIT_INDEX_FILE"] = index_file
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=env,
    )


def _git_absolute_git_dir(cwd: str) -> str:
    """The absolute git directory of the repository/worktree at *cwd*."""
    proc = _run_git_plumbing(cwd, ["rev-parse", "--absolute-git-dir"])
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(
            "Cannot resolve the git directory for the finish gate's "
            f"temporary index at {cwd}: "
            f"{proc.stderr.strip() or 'git rev-parse failed'}"
        )
    return proc.stdout.strip()


def _is_git_worktree(cwd: str) -> bool:
    """Whether *cwd* is inside a git work tree.

    The precondition of the finish gate's would-be-commit staging: a directory
    outside a work tree has no commit tree to attribute a verdict to, so the
    gate degrades to a plain suite run instead of refusing to run at all
    (SA-0MVVKJGKP003QVNM).
    """
    proc = _run_git_plumbing(cwd, ["rev-parse", "--is-inside-work-tree"])
    return proc.returncode == 0 and proc.stdout.strip() == "true"


# ---------------------------------------------------------------------------
# Build-originated composition churn (SA-0MVZE164P0048306)
# ---------------------------------------------------------------------------
#: Git mode of a submodule entry (a "gitlink") in a tree or an index.
GITLINK_MODE = "160000"

#: Path prefix of the finish run's own bookkeeping surface. A tracked change
#: there (``wl`` comments, status and cache writes) is never build composition
#: churn and is never restored by the build-churn stage.
CHURN_EXEMPT_PREFIX = ".worklog/"

#: Prefix of the ``engine:sync`` lines a game repo's build prints to stdout.
#: They are the only record that the build advanced the engine before it
#: compiled (SA-0MW06FLFR009F0P4).
ENGINE_SYNC_LINE_PREFIX = "engine:sync"

#: Maximum number of ``engine:sync`` lines quoted in a build-failure comment.
ENGINE_SYNC_MAX_LINES = 20

#: Characters of a build's stdout/stderr kept in a build-failure comment.
BUILD_OUTPUT_TAIL_CHARS = 500


def _parse_gitlink_entries(output: str, sha_field: int = 2) -> dict[str, str]:
    """Parse NUL-separated ``ls-tree``/``ls-files --stage`` output into a map.

    Both plumbing commands emit ``<mode> SP <field> SP <field> TAB <path>`` but
    with the SHA in a different position: ``ls-tree`` is ``<mode> <type>
    <sha>`` (the SHA is the third field), ``ls-files --stage`` is ``<mode>
    <sha> <stage>`` (the SHA is the second). Only ``160000`` (gitlink) entries
    are kept — they are the submodule pointers, i.e. the engine pin a commit
    records.

    Args:
        output: Raw NUL-terminated output of ``git ls-tree -r -z`` (default
            ``sha_field``) or ``git ls-files --stage -z`` (``sha_field=1``).
        sha_field: Zero-based index of the field holding the recorded commit.

    Returns:
        Mapping of submodule path to the commit SHA it records.
    """
    gitlinks: dict[str, str] = {}
    for entry in output.split("\0"):
        if not entry.strip():
            continue
        meta, _, path = entry.partition("\t")
        fields = meta.split()
        if len(fields) > sha_field and fields[0] == GITLINK_MODE and path:
            gitlinks[path] = fields[sha_field]
    return gitlinks


def _head_gitlinks(cwd: str) -> dict[str, str]:
    """Map every gitlink path recorded in ``HEAD`` to its commit.

    This is the *committed* composition — for the game repos that compose the
    Tableau Card Engine at ``core`` it is the engine pin the commit records.
    Empty when ``HEAD`` is unborn or unreadable: there is then no recorded
    composition to restore.

    Args:
        cwd: Worktree root.

    Returns:
        Mapping of submodule path to the commit ``HEAD`` records.
    """
    proc = _run_git_plumbing(cwd, ["ls-tree", "-r", "-z", "HEAD"])
    if proc.returncode != 0:
        return {}
    return _parse_gitlink_entries(proc.stdout)


def _index_gitlinks(cwd: str) -> dict[str, str]:
    """Map every gitlink path currently staged in the real index to its commit.

    A build (``engine:sync``) stages the gitlink it advanced, so the index is
    part of the pre/post-build state comparison.

    Args:
        cwd: Worktree root.

    Returns:
        Mapping of submodule path to the commit the index stages.
    """
    proc = _run_git_plumbing(cwd, ["ls-files", "--stage", "-z"])
    if proc.returncode != 0:
        return {}
    return _parse_gitlink_entries(proc.stdout, sha_field=1)


def _tracked_dirty_paths(cwd: str) -> set[str]:
    """Tracked paths whose state differs from ``HEAD``, staged or not.

    Uses ``git diff HEAD`` (index *and* working tree against the commit), so a
    build that stages its own output is visible here just like one that only
    rewrites the working tree. Untracked files are deliberately excluded: a
    build output the project does not track is not a composition path.

    The finish run's own bookkeeping surface (``.worklog/`` — ``wl`` comments,
    status and cache writes) is excluded: it is never composition, and a
    concurrent bookkeeping write must never be mistaken for build churn and
    restored.

    Args:
        cwd: Worktree root.

    Returns:
        Set of repository-relative paths (empty when the query fails).
    """
    proc = _run_git_plumbing(
        cwd, ["diff", "--name-only", "--no-renames", "-z", "HEAD"]
    )
    if proc.returncode != 0:
        return set()
    return {
        path
        for path in proc.stdout.split("\0")
        if path and not path.startswith(CHURN_EXEMPT_PREFIX)
    }


def _path_state_token(
    cwd: str, path: str, gitlinks: dict[str, str]
) -> str:
    """A token that changes only when a dirty path's content really changes.

    Distinguishes a path the item modified before the build (dirty in the
    baseline) from one that changed *during* the build: for a submodule the
    token is its checked-out commit, for a regular path the hash of its working
    content. A path the build changed *and* the item had already modified is
    therefore detectable, so it is never mistaken for pure build output.

    Args:
        cwd: Worktree root.
        path: Repository-relative path to fingerprint.
        gitlinks: Known submodule paths for this checkout.

    Returns:
        An opaque state token string.
    """
    if path in gitlinks:
        proc = _run_git_plumbing(str(Path(cwd) / path), ["rev-parse", "HEAD"])
        if proc.returncode == 0 and proc.stdout.strip():
            return f"gitlink:{proc.stdout.strip()}"
        return "gitlink:unresolved"
    if not (Path(cwd) / path).exists():
        return "absent"
    proc = _run_git_plumbing(cwd, ["hash-object", "--", path])
    if proc.returncode == 0 and proc.stdout.strip():
        return f"blob:{proc.stdout.strip()}"
    return "unhashed"


def _build_churn_baseline(cwd: str) -> dict[str, Any]:
    """Snapshot the item's own change set immediately before the build runs.

    Whatever differs from this snapshot after the build was produced *by the
    build*, not by the item — the distinction AC1/AC2 of SA-0MVZE164P0048306
    turn on. Taken after the (optional) refactor step and before ``run_build``,
    so refactor edits count as the item's own change.

    Args:
        cwd: Worktree root.

    Returns:
        ``{taken_at, gitlinks, index_gitlinks, dirty_paths}`` where
        ``dirty_paths`` maps every tracked dirty path to its
        :func:`_path_state_token`, ``gitlinks`` is ``HEAD``'s recorded
        composition and ``index_gitlinks`` the composition staged at build
        start (the item's own pin change, when it made one). Outside a git
        work tree all three are empty (the churn stage is then a no-op).
    """
    gitlinks = _head_gitlinks(cwd)
    dirty = sorted(_tracked_dirty_paths(cwd))
    known = {**_index_gitlinks(cwd), **gitlinks}
    return {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "gitlinks": gitlinks,
        "index_gitlinks": _index_gitlinks(cwd),
        "dirty_paths": {
            path: _path_state_token(cwd, path, known) for path in dirty
        },
    }


def _composed_engine_state(
    cwd: str, commit_tree: str | None = None
) -> dict[str, Any]:
    """Record the composition a gate verdict's suite ran against.

    For every recorded gitlink this resolves the commit the submodule
    **checkout** is actually at — the composed engine the suite saw — and
    compares it with the gitlink the *commit* records. That pair is the
    provenance AC4 of SA-0MVZE164P0048306 requires alongside the
    would-be-commit tree: a verdict now states which engine SHA it tested, not
    merely which tree.

    Args:
        cwd: Worktree root.
        commit_tree: The would-be-commit tree whose gitlinks are authoritative
            (``git write-tree`` of the staged change set, AC3). ``None``
            compares against ``HEAD`` — the committed composition, used by the
            build-churn step which runs before the gate stages anything.

    Returns:
        ``{"submodules": [{"submodule", "commit", "recorded_gitlink",
        "source", "matches_recorded"}], "composition": "none"|"submodules"}``.
        An unresolved checkout is reported with ``commit: None`` rather than
        omitted, so a broken composition is visible in the verdict.
    """
    if commit_tree:
        proc = _run_git_plumbing(cwd, ["ls-tree", "-r", "-z", commit_tree])
        recorded_map = (
            _parse_gitlink_entries(proc.stdout) if proc.returncode == 0 else {}
        )
        source = "would-be-commit tree"
    else:
        recorded_map = _head_gitlinks(cwd)
        source = "HEAD"
    entries: list[dict[str, Any]] = []
    for path, recorded in sorted(recorded_map.items()):
        proc = _run_git_plumbing(str(Path(cwd) / path), ["rev-parse", "HEAD"])
        resolved = proc.stdout.strip() if proc.returncode == 0 else ""
        entries.append(
            {
                "submodule": path,
                "commit": resolved or None,
                "recorded_gitlink": recorded,
                "source": source,
                "matches_recorded": bool(resolved) and resolved == recorded,
            }
        )
    return {
        "submodules": entries,
        "composition": "submodules" if entries else "none",
    }


def _restore_build_churn_path(
    cwd: str, path: str, gitlinks: dict[str, str], known: dict[str, str]
) -> dict[str, Any]:
    """Restore one build-originated path to the state ``HEAD`` records.

    A regular path is restored from ``HEAD`` (``git checkout HEAD -- <path>``
    restores the index entry and the working file). A gitlink needs both
    halves: ``git reset HEAD -- <path>`` restores the recorded pointer in the
    index and the submodule checkout is moved back to that commit, which is
    what makes the *tested* composition equal the *committed* one (AC3 of
    SA-0MVZE164P0048306) instead of leaving the suite on the advanced engine.

    Nothing is ever discarded by force: a submodule with local modifications
    fails the checkout and is reported as unrestorable rather than cleaned —
    the local work may be the item's own.

    Args:
        cwd: Worktree root.
        path: Repository-relative path to restore.
        gitlinks: ``HEAD``'s recorded composition.
        known: Every known submodule path (HEAD plus the current index).

    Returns:
        ``{path, kind, restored}`` plus ``commit`` when restored, or
        ``error``/``reason`` when it was not.
    """
    recorded = gitlinks.get(path)
    if recorded is not None:
        failures: list[str] = []
        reset = _run_git_plumbing(cwd, ["reset", "-q", "HEAD", "--", path])
        if reset.returncode != 0:
            failures.append(
                f"git reset HEAD -- {path}: "
                f"{reset.stderr.strip() or 'failed'}"
            )
        checkout = _run_git_plumbing(
            str(Path(cwd) / path), ["checkout", "-q", "--detach", recorded]
        )
        if checkout.returncode != 0:
            failures.append(
                f"git -C {path} checkout --detach {recorded}: "
                f"{checkout.stderr.strip() or 'failed'}"
            )
            sync = _run_git_plumbing(
                cwd, ["submodule", "update", "--init", "--checkout", "--", path]
            )
            if sync.returncode != 0:
                failures.append(
                    f"git submodule update --init --checkout {path}: "
                    f"{sync.stderr.strip() or 'failed'}"
                )
            elif path not in _tracked_dirty_paths(cwd):
                failures.clear()
        if not failures and path in _tracked_dirty_paths(cwd):
            failures.append(
                f"{path} is still recorded as modified after the restore "
                "(the submodule checkout does not match the gitlink HEAD "
                "records)"
            )
        if failures:
            return {
                "path": path,
                "kind": "submodule",
                "restored": False,
                "error": "; ".join(failures),
            }
        return {
            "path": path,
            "kind": "submodule",
            "restored": True,
            "commit": recorded,
        }

    checkout = _run_git_plumbing(cwd, ["checkout", "HEAD", "--", path])
    if checkout.returncode == 0:
        return {"path": path, "kind": "file", "restored": True}
    if path in known:
        # A gitlink the build staged but HEAD does not record: there is no
        # committed state to restore it to, so it can only be left in place.
        return {
            "path": path,
            "kind": "submodule",
            "restored": False,
            "reason": "not_in_head",
            "error": checkout.stderr.strip(),
        }
    head_blob = _run_git_plumbing(cwd, ["cat-file", "-e", f"HEAD:{path}"])
    if head_blob.returncode != 0:
        return {
            "path": path,
            "kind": "file",
            "restored": False,
            "reason": "not_in_head",
            "error": checkout.stderr.strip(),
        }
    return {
        "path": path,
        "kind": "file",
        "restored": False,
        "error": checkout.stderr.strip() or "git checkout HEAD failed",
    }


def _exclude_build_originated_churn(
    cwd: str, baseline: dict[str, Any]
) -> dict[str, Any]:
    """Undo the tracked churn the build produced, keeping the item's own work.

    A build such as this repo's ``npm run build`` (``engine:sync`` first) can
    advance the engine submodule and rewrite the pin when the tracked engine
    branch tip has moved, staging both. Left alone, finish's ``git add -A``
    commits that bump — an engine change the item never originated — and the
    gate tests a composition the commit does not record (SA-0MVZE164P0048306).

    The rule is *conditional on origin*, never an unconditional ignore list:
    a path restored here must have been **clean before the build** and dirty
    after it. A path the item already had dirty is part of the item's own
    change set and is preserved — even when the build also touched it
    (``item_modified_during_build``), because restoring it would discard the
    item's work.

    Args:
        cwd: Worktree root.
        baseline: The :func:`_build_churn_baseline` taken before the build.

    Returns:
        A report dict: ``baseline_dirty_paths``, ``build_originated``,
        ``item_modified_during_build``, ``restored``, ``excluded_from_commit``,
        ``left_in_place``, ``restore_failed``, ``success``/``reason`` and
        ``composed_engine``. ``restore_failed`` non-empty means finish must
        refuse: the commit would otherwise record a composition the item did
        not originate (or, for a file, a state that could not be restored).
    """
    before = baseline.get("dirty_paths") or {}
    gitlinks = dict(baseline.get("gitlinks") or {})
    known = {**_index_gitlinks(cwd), **gitlinks}

    originated: list[str] = []
    item_modified: list[str] = []
    for path in sorted(_tracked_dirty_paths(cwd)):
        if path not in before:
            originated.append(path)
        elif before[path] != _path_state_token(cwd, path, known):
            item_modified.append(path)

    restored: list[dict[str, Any]] = []
    left_in_place: list[dict[str, Any]] = []
    restore_failed: list[dict[str, Any]] = []
    for path in originated:
        outcome = _restore_build_churn_path(cwd, path, gitlinks, known)
        if outcome.get("restored"):
            restored.append(outcome)
        elif outcome.get("reason") == "not_in_head":
            left_in_place.append(outcome)
        else:
            restore_failed.append(outcome)

    for entry in restored:
        LOG.info(
            "Finish gate: excluded build-originated churn %s (%s)",
            entry["path"],
            entry["kind"],
        )
    for entry in item_modified:
        LOG.info(
            "Finish gate: kept %s (modified by the item and touched by the "
            "build; never restored)",
            entry,
        )

    report: dict[str, Any] = {
        "baseline_dirty_paths": sorted(before),
        "build_originated": originated,
        "item_modified_during_build": item_modified,
        "restored": restored,
        "excluded_from_commit": [entry["path"] for entry in restored],
        "left_in_place": left_in_place,
        "restore_failed": restore_failed,
        "success": not restore_failed,
        "composed_engine": _composed_engine_state(cwd),
    }
    if restore_failed:
        report["reason"] = "restore_failed"
    return report


def _build_churn_refusal_message(churn: dict[str, Any]) -> str:
    """Actionable refusal for build churn that could not be excluded.

    Never silent and never forced: the offending paths, the git error and the
    two remedies (clean the submodule, or make the change part of the item's
    own change set before the build) are all named.

    Args:
        churn: The :func:`_exclude_build_originated_churn` report.

    Returns:
        The message recorded on the work item and returned to the caller.
    """
    details = "\n".join(
        f"  - {entry['path']} ({entry['kind']}): {entry.get('error', 'failed')}"
        for entry in churn.get("restore_failed") or []
    )
    return (
        "Finish refused: build-originated churn could not be excluded from "
        "the item's commit.\n"
        f"{details}\n"
        "The build produced these tracked changes, so committing them would "
        "record a pin/gitlink bump this item did not originate, and the "
        "suite would not run against the composition the commit records.\n"
        "Resolve the reported git error (for a submodule: clean or restore "
        "its checkout) and re-run `implement.py finish`. If the change is "
        "genuinely this item's, make it part of the change set *before* the "
        "build (stage or modify it yourself) so it is preserved."
    )


def _engine_lag(cwd: str, baseline: dict[str, Any]) -> dict[str, Any]:
    """Report a failed build's engine advance against the item's pinned engine.

    A game repo's ``npm run build`` runs ``engine:sync`` first, which advances
    the ``core`` submodule to the tracked-branch tip before the rest of the
    build compiles the item's source. If the item predates a breaking engine
    API change the build then fails with a compile error that has nothing to
    do with the item's own change — and the failure message carried no trace
    of the advance, so an engine-induced failure looked like a source failure
    (SA-0MW06FLFR009F0P4).

    The data was already available: :func:`_build_churn_baseline` records the
    pre-build gitlink (the item's pinned engine) and the submodule checkout
    ``HEAD`` is the engine the build actually compiled against. Comparing them
    is additive reporting only — nothing is restored or staged here.

    Args:
        cwd: Worktree root.
        baseline: The :func:`_build_churn_baseline` taken before the build.

    Returns:
        ``{submodules: [{submodule, pinned_sha, composed_sha, advanced}],
        advanced}``. ``pinned_sha`` is the composition staged at build start —
        the item's own pin when it changed the composition itself, else the
        committed gitlink — so an item-originated composition change is never
        reported as a build advance. A repo without a recorded submodule
        composition reports an empty ``submodules`` list, and an unresolved
        submodule checkout is reported with ``composed_sha: None`` and
        ``advanced: false`` — a broken composition is never an advance.
    """
    gitlinks = dict(baseline.get("gitlinks") or {})
    pins = {**gitlinks, **(baseline.get("index_gitlinks") or {})}
    submodules: list[dict[str, Any]] = []
    for path, recorded in sorted(gitlinks.items()):
        pinned = pins.get(path, recorded)
        proc = _run_git_plumbing(str(Path(cwd) / path), ["rev-parse", "HEAD"])
        composed = proc.stdout.strip() if proc.returncode == 0 else ""
        submodules.append(
            {
                "submodule": path,
                "pinned_sha": pinned,
                "composed_sha": composed or None,
                "advanced": bool(composed) and composed != pinned,
            }
        )
    return {
        "submodules": submodules,
        "advanced": any(entry["advanced"] for entry in submodules),
    }


def _engine_sync_lines(stdout: str, limit: int = ENGINE_SYNC_MAX_LINES) -> list[str]:
    """The ``engine:sync`` lines a build printed to stdout.

    ``engine:sync`` logs the advance (``engine:sync: core updated <from> →
    <to>``) and the pin rewrite to **stdout**, where the old build-failure
    comment — which posted only ``stderr`` — could never show them.

    Args:
        stdout: The build's captured stdout.
        limit: Maximum number of lines kept.

    Returns:
        The matching lines, in order. Empty when the build produced no
        ``engine:sync`` output at all (Python-only repos, a skipped build, or
        a sync that failed before printing).
    """
    return [
        line.strip()
        for line in (stdout or "").splitlines()
        if line.strip().lower().startswith(ENGINE_SYNC_LINE_PREFIX)
    ][:limit]


def _build_output_tail(output: str, limit: int = BUILD_OUTPUT_TAIL_CHARS) -> str:
    """The last *limit* characters of a build output stream.

    Args:
        output: The captured stdout or stderr.
        limit: Maximum number of characters kept.

    Returns:
        The tail, prefixed with an elision marker when truncated. Empty for
        empty input.
    """
    text = (output or "").strip()
    if len(text) <= limit:
        return text
    return "…(truncated)\n" + text[-limit:]


def _engine_lag_diagnosis(engine_lag: dict[str, Any]) -> str:
    """The operator-facing line(s) naming both SHAs of every advanced engine.

    Args:
        engine_lag: The :func:`_engine_lag` report.

    Returns:
        One bullet per advanced submodule, empty when nothing advanced.
    """
    return "\n".join(
        f"- {entry['submodule']}: build ran against engine "
        f"{entry['composed_sha']}; the item pinned {entry['pinned_sha']} — "
        "the advance is not part of this item's commit"
        for entry in engine_lag.get("submodules") or []
        if entry.get("advanced")
    )


def _build_failure_message(
    build_result: dict[str, Any], engine_lag: dict[str, Any]
) -> str:
    """The work-item comment a failed finish build posts.

    Records the exit code, the engine diagnosis when the build advanced the
    engine past the item's pin, the ``engine:sync`` lines and the tails of
    **both** streams — stdout is where ``engine:sync`` reports the advance, so
    stderr alone can leave the operator without the cause of a compile error
    (SA-0MW06FLFR009F0P4).

    Args:
        build_result: The :func:`run_build` result.
        engine_lag: The :func:`_engine_lag` report for the failed build.

    Returns:
        The comment body (Markdown).
    """
    sections = [
        (
            "Build failed during finish phase "
            f"(exit code {build_result['exit_code']})."
        )
    ]
    if engine_lag.get("advanced"):
        sections.append(
            "**Engine advanced past the item's pin during the build.** The "
            "compile errors below may come from engine APIs this item "
            "predates:\n" + _engine_lag_diagnosis(engine_lag)
        )
        sync_lines = _engine_sync_lines(build_result.get("stdout") or "")
        if sync_lines:
            sections.append(
                "**engine:sync output**\n```\n" + "\n".join(sync_lines) + "\n```"
            )
    stdout_tail = _build_output_tail(build_result.get("stdout") or "")
    if stdout_tail:
        sections.append("**stdout (tail)**\n```\n" + stdout_tail + "\n```")
    stderr_tail = _build_output_tail(build_result.get("stderr") or "")
    if stderr_tail:
        sections.append("**stderr (tail)**\n```\n" + stderr_tail + "\n```")
    return "\n\n".join(sections)


@dataclass
class WouldBeCommitIndex:
    """The temporary index the finish gate tests the would-be-commit tree in.

    ``tree`` is the ``git write-tree`` SHA of the change set staged into the
    temporary index — the tree the suite therefore ran against. The real index
    and the working tree are never modified while the gate runs.
    """

    worktree: str
    index_path: str
    real_index: str
    tree: str
    staged_paths: list[str]

    def summary(self) -> dict[str, Any]:
        """Report-shaped summary of the tested tree (recorded by finish)."""
        return {
            "tree": self.tree,
            "index_path": self.index_path,
            "real_index": self.real_index,
            "staged_paths": list(self.staged_paths),
            "staged_count": len(self.staged_paths),
        }

    def verify_unchanged(self) -> dict[str, Any]:
        """Re-derive the temporary index's tree after the suite has run.

        A fixture (or a child ``git`` invocation) that honoured the inherited
        ``GIT_INDEX_FILE`` and staged into it would change the tree the gate
        believes it tested. Comparing the tree before and after makes that
        unattributable verdict detectable instead of silently accepted.
        """
        proc = _run_git_plumbing(
            self.worktree, ["write-tree"], index_file=self.index_path
        )
        current = proc.stdout.strip()
        return {
            "unchanged": proc.returncode == 0 and current == self.tree,
            "tree": current,
            "expected": self.tree,
        }


@contextlib.contextmanager
def would_be_commit_index(cwd: str) -> Iterator[WouldBeCommitIndex]:
    """Stage the pending change set into a copy of the real index.

    The finish gate must validate the exact tree the commit will produce, not
    the loose working tree (SA-0MVL2NH1G0094FA2). Before the suite runs, the
    real index is copied to a temporary file, the change set is applied to the
    copy (``git add -A`` under ``GIT_INDEX_FILE``), and ``GIT_INDEX_FILE`` is
    exported for the duration so every child ``git`` command the suite spawns
    — including ``git ls-files`` guards inside tests — sees the staged file
    set. ``GIT_INDEX_FILE`` survives the test skill's repository-override
    scrub (F3, SA-0MVQHV9Y3001RNNW) for exactly that reason.

    The temporary index is removed and the previous ``GIT_INDEX_FILE`` value
    restored on **every** exit path (success, gate failure, fix-loop abort,
    exception), and the real index is never written: a failed gate leaves no
    residual staging (F5, SA-0MVQHVCRE0070YO6).

    Raises:
        RuntimeError: when the index cannot be prepared or the change set
            cannot be staged — with no temporary index and no environment
            change left behind.
    """
    global _gate_index_global

    git_dir = Path(_git_absolute_git_dir(cwd))
    real_index = git_dir / "index"
    fd, index_path = tempfile.mkstemp(
        prefix=GATE_INDEX_PREFIX, suffix=GATE_INDEX_SUFFIX, dir=str(git_dir)
    )
    os.close(fd)
    _gate_index_global = index_path
    previous_index_env = os.environ.get("GIT_INDEX_FILE")
    try:
        if real_index.exists():
            shutil.copyfile(real_index, index_path)
        else:
            read_tree = _run_git_plumbing(
                cwd, ["read-tree", "HEAD"], index_file=index_path
            )
            if read_tree.returncode != 0:
                raise RuntimeError(
                    "Cannot seed the finish gate's temporary index from "
                    f"HEAD: {read_tree.stderr.strip() or 'git read-tree failed'}"
                )

        add = _run_git_plumbing(cwd, ["add", "-A"], index_file=index_path)
        if add.returncode != 0:
            raise RuntimeError(
                "Cannot stage the change set in the finish gate's temporary "
                f"index: {add.stderr.strip() or 'git add -A failed'}"
            )

        write_tree = _run_git_plumbing(cwd, ["write-tree"], index_file=index_path)
        if write_tree.returncode != 0 or not write_tree.stdout.strip():
            raise RuntimeError(
                "Cannot compute the would-be-commit tree of the finish gate's "
                f"temporary index: "
                f"{write_tree.stderr.strip() or 'git write-tree failed'}"
            )

        staged = _run_git_plumbing(
            cwd,
            ["diff", "--cached", "--name-only", "HEAD"],
            index_file=index_path,
        )

        os.environ["GIT_INDEX_FILE"] = index_path
        LOG.info(
            "Finish gate: testing the would-be-commit tree %s "
            "(temporary index %s)",
            write_tree.stdout.strip(),
            index_path,
        )

        yield WouldBeCommitIndex(
            worktree=cwd,
            index_path=index_path,
            real_index=str(real_index),
            tree=write_tree.stdout.strip(),
            staged_paths=[
                line.strip() for line in staged.stdout.splitlines() if line.strip()
            ],
        )
    finally:
        if previous_index_env is None:
            os.environ.pop("GIT_INDEX_FILE", None)
        else:
            os.environ["GIT_INDEX_FILE"] = previous_index_env
        _remove_gate_index(index_path)
        _gate_index_global = None


def run_gate_suite(
    cwd: str, scope: str = "changed", base_ref: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the finish gate's suite against the would-be-commit tree.

    Returns ``(test_result, summary)``: the normal ``run_tests`` result and the
    temp-index summary (recorded under ``steps.would_be_commit``). When the
    suite mutated the temporary index the verdict is invalid — a test staged
    into the inherited ``GIT_INDEX_FILE`` instead of its own repository's
    index — so the result is forced red with an explanatory notice rather than
    being attributed to a tree that was never the one tested.

    *cwd* outside a git work tree has no would-be-commit tree to verify: the
    suite runs plainly and the summary reports ``available: False`` with the
    stable ``not_a_git_worktree`` reason (SA-0MVVKJGKP003QVNM).

    Verdict provenance (SA-0MVQHVJEX0071R7G): the returned ``test_result``
    carries ``tree`` (the absolute worktree path the suite ran in) and
    ``git_fingerprint`` (the would-be-commit tree SHA the verdict is bound to),
    alongside the ``cached``/``cache_entry`` provenance the suite run reported.
    Every verdict therefore states both *which tree* it belongs to, whether it
    was produced or served, and (SA-0MVZE164P0048306 AC4) which composed
    engine SHA it ran against (``composed_engine`` — the resolved submodule
    checkout commit vs the gitlink ``HEAD`` records).

    Returns:
        Tuple of ``(test_result, summary)``; both carry ``composed_engine``
        when the composition is resolvable, ``None`` outside a git work tree.
    """
    tested_tree = str(Path(cwd).resolve())
    if not _is_git_worktree(cwd):
        notice = (
            "finish gate tree check unavailable: "
            f"{cwd} is not inside a git work tree, so there is no "
            "would-be-commit tree to verify; the suite ran against the "
            "working tree as-is."
        )
        LOG.warning("Finish gate: %s", notice)
        result = run_tests(cwd, scope=scope, base_ref=base_ref)
        # Attribute the verdict to the directory the suite ran in: there is no
        # would-be-commit tree to fingerprint outside a work tree.
        result["tree"] = tested_tree
        result["git_fingerprint"] = None
        result["composed_engine"] = None
        return result, {
            "available": False,
            "reason": "not_a_git_worktree",
            "notice": notice,
            "staged_paths": [],
            "composed_engine": None,
        }

    with would_be_commit_index(cwd) as gate_index:
        # Bind verdict acceptance to the would-be-commit tree: the cache key
        # is the tree SHA, so a verdict recorded for any other tree (or a
        # legacy entry without provenance) misses and the suite really runs
        # (SA-0MVQHVHXI007PQAB).
        composed_engine = _composed_engine_state(cwd, gate_index.tree)
        result = run_tests(
            cwd, scope=scope, base_ref=base_ref, fingerprint=gate_index.tree
        )
        integrity = gate_index.verify_unchanged()
        summary = gate_index.summary()

    summary["index_unchanged"] = integrity["unchanged"]
    summary["composed_engine"] = composed_engine
    # Attribute the verdict to the tree it was bound to: the absolute worktree
    # the suite ran in and the would-be-commit tree SHA its cache key used
    # (SA-0MVQHVJEX0071R7G). Recorded on the run result in place so callers
    # keep the result object the suite returned. The composed engine SHA is
    # the composition the suite actually ran against (SA-0MVZE164P0048306
    # AC4).
    result["tree"] = tested_tree
    result["git_fingerprint"] = gate_index.tree
    result["composed_engine"] = composed_engine
    if integrity["unchanged"]:
        return result, summary

    notice = (
        "gate evidence invalid: the temporary index changed while the suite "
        f"ran (tested tree {integrity['expected']}, index now "
        f"{integrity['tree'] or 'unreadable'}). A test staged into the "
        "inherited GIT_INDEX_FILE instead of its own repository's index; the "
        "verdict cannot be attributed to the would-be-commit tree. Fixture "
        "git commands must use a hermetic environment (for example "
        "shared.git_sandbox.fixture_git_env())."
    )
    LOG.error("Finish gate: %s", notice)
    return (
        {
            **result,
            "success": False,
            "notices": [*(result.get("notices") or []), notice],
        },
        summary,
    )


def _gate_attempt_record(
    attempt: int, test_result: dict[str, Any]
) -> dict[str, Any]:
    """One finish-gate test attempt, with the provenance of its verdict.

    Every attempt records the tree the suite ran against (``tree``), the
    would-be-commit tree SHA the verdict is bound to (``git_fingerprint``),
    whether the verdict was served from the cache (``cached``, plus
    ``cache_entry`` metadata — command / completed_at / scope — when it was)
    and the composed engine SHA the suite ran against (``composed_engine``,
    SA-0MVZE164P0048306 AC4). A green verdict is therefore self-describing: it
    names the state whose suite ran, so a stale or foreign cached green cannot
    be mistaken for it (SA-0MVQHVJEX0071R7G).
    """
    record: dict[str, Any] = {
        "attempt": attempt,
        "success": test_result["success"],
        "failures": test_result.get("failures", []),
        "skipped": test_result.get("skipped", False),
        "tooling": test_result.get("tooling"),
        "scope": test_result.get("scope", "changed"),
        "tree": test_result.get("tree"),
        "git_fingerprint": test_result.get("git_fingerprint"),
        "cached": bool(test_result.get("cached", False)),
        "composed_engine": test_result.get("composed_engine"),
    }
    entry = test_result.get("cache_entry")
    if record["cached"] and entry:
        record["cache_entry"] = dict(entry)
    return record


def _verdict_provenance_lines(report: dict[str, Any]) -> list[str]:
    """Completion-comment lines describing the finish gate's verdict provenance.

    The comment on the work item must carry what the JSON report carries: the
    tree the accepted verdict belongs to, its would-be-commit fingerprint and
    whether the suite ran for it or the verdict was served from the cache
    (SA-0MVQHVJEX0071R7G).
    """
    attempts = list((report.get("steps") or {}).get("tests") or [])
    if not attempts:
        return ["- Test gate verdict: not recorded (no test step ran)"]
    final = attempts[-1]
    lines = [
        "- Test gate verdict:",
        f"  - Tree: {final.get('tree')}",
        f"  - Fingerprint: {final.get('git_fingerprint')}",
        f"  - Cached: {'true' if final.get('cached') else 'false'}",
    ]
    entry = final.get("cache_entry") or {}
    if entry:
        lines.append(
            f"  - Cache entry: command {entry.get('command')}, "
            f"completed_at {entry.get('completed_at')}, "
            f"scope {entry.get('scope')}, "
            f"fingerprint_kind {entry.get('fingerprint_kind')}"
        )
    # The composed engine SHA the accepted verdict's suite ran against
    # (SA-0MVZE164P0048306 AC4): the resolved submodule checkout commit vs the
    # gitlink ``HEAD`` records, so the green is attributable to a specific
    # engine composition, not merely a tree.
    composed = final.get("composed_engine") or {}
    submodules = list(composed.get("submodules") or [])
    if submodules:
        for sub in submodules:
            lines.append(
                f"  - Composed engine: {sub.get('submodule')}@"
                f"{sub.get('commit')} (recorded gitlink "
                f"{sub.get('recorded_gitlink')} in "
                f"{sub.get('source')}, "
                "match: "
                f"{'true' if sub.get('matches_recorded') else 'false'})"
            )
    else:
        lines.append(
            "  - Composed engine: none (the recorded tree has no submodule "
            "composition)"
        )
    if len(attempts) > 1:
        lines.append(f"  - Attempts: {len(attempts)}")
    return lines


def stage_tested_tree(cwd: str, tested_tree: str | None) -> dict[str, Any]:
    """Stage the change set in the **real** index and prove it is the tested tree.

    Called only once the gate is green: the change set is staged exactly as
    :func:`git_commit` does (``git add -A``) and ``git write-tree`` is compared
    with the tree the suite ran against, so a commit can never be created for
    a tree the suite did not test. On a mismatch the just-staged change set is
    unstaged again, leaving no residual staging (the F5 abort contract).

    Returns a report dict with ``success``, ``tree``, ``expected`` and, on
    failure, an actionable ``message``.
    """
    add = _run_git_plumbing(cwd, ["add", "-A"])
    if add.returncode != 0:
        return {
            "success": False,
            "expected": tested_tree,
            "message": "Could not stage the change set in the real index: "
            f"{add.stderr.strip() or 'git add -A failed'}",
        }

    write_tree = _run_git_plumbing(cwd, ["write-tree"])
    if write_tree.returncode != 0 or not write_tree.stdout.strip():
        return {
            "success": False,
            "expected": tested_tree,
            "message": "Could not compute the committed tree: "
            f"{write_tree.stderr.strip() or 'git write-tree failed'}",
        }

    actual = write_tree.stdout.strip()
    if tested_tree and actual != tested_tree:
        # Leave no residual staging: the gate refused this tree, so the real
        # index must be as untouched as an aborted gate leaves it.
        _run_git_plumbing(cwd, ["reset", "--quiet"])
        return {
            "success": False,
            "expected": tested_tree,
            "tree": actual,
            "message": (
                "The commit's tree does not match the tree the finish gate "
                f"tested (tested {tested_tree}, would commit {actual}). The "
                "change set was unstaged again and no commit was created; "
                "re-run `implement.py finish` after investigating the tree "
                "drift (a build step or a concurrent writer changed files "
                "after the gate ran)."
            ),
        }
    return {
        "success": True,
        "tree": actual,
        "expected": tested_tree,
        "verified": bool(tested_tree) and actual == tested_tree,
    }


def git_commit(cwd: str, message: str) -> bool:
    """Stage all changes and commit in the given directory.

    Args:
        cwd: Working directory (worktree root).
        message: Commit message.

    Returns:
        True if the commit succeeded.
    """
    # Stage all changes (surgical: only what was modified)
    add_result = run_cmd(["git", "add", "-A"], cwd=cwd, check=False)
    if add_result.returncode != 0:
        LOG.error("git add failed: %s", add_result.stderr.strip())
        return False

    # Check if there's anything to commit
    diff_result = run_cmd(
        ["git", "diff", "--cached", "--quiet"], cwd=cwd, check=False
    )
    if diff_result.returncode == 0:
        LOG.info("No changes to commit (clean working tree)")
        return True

    commit_result = run_cmd(
        ["git", "commit", "-m", message],
        cwd=cwd,
        check=False,
    )
    if commit_result.returncode != 0:
        LOG.error("git commit failed: %s", commit_result.stderr.strip())
        return False
    return True


class PushTimeoutError(RuntimeError):
    """Raised when a push times out, carrying commit/branch info for recovery."""

    def __init__(self, commit_hash: str, branch: str, timeout: int) -> None:
        self.commit_hash = commit_hash
        self.branch = branch
        self.timeout = timeout
        super().__init__(
            f"Push to dev timed out after {timeout}s. "
            f"Commit {commit_hash} on branch {branch} is local. "
            f"Push manually: git push origin {branch}:refs/heads/dev"
        )


class PushNonFastForwardError(RuntimeError):
    """Raised when a push fails due to non-fast-forward (dev advanced concurrently).

    Carries commit_hash, branch, and remote stderr so the caller can rebase
    the feature branch onto origin/dev and retry the push.
    """

    def __init__(
        self, commit_hash: str, branch: str, stderr: str, rebase_cmd: str,
    ) -> None:
        self.commit_hash = commit_hash
        self.branch = branch
        self.stderr = stderr
        self.rebase_cmd = rebase_cmd
        super().__init__(
            f"Push to dev failed (non-fast-forward): {stderr}. "
            f"Commit {commit_hash} on branch {branch}. "
            f"Retry: {rebase_cmd}"
        )


class PushSubjectMismatchError(RuntimeError):
    """Raised when the dev push would gate a tree other than the pushed commit.

    ``finish`` pushes from the worktree that holds its commit so the pre-push
    full-suite gate measures the pushed tree. When that checkout cannot be the
    subject (missing worktree, not a git working tree, its ``HEAD`` is a
    different commit, or it has tracked uncommitted changes that a recovery
    rebase cannot carry), pushing anyway would let the gate verify — and
    cache — a tree the pushed commit does not describe. The guard therefore
    refuses before any push, naming the commit, branch, worktree and the
    actual ``HEAD`` so a human can recover (SA-0MVTFMB7K002Y1F3 AC2/AC3).
    """

    def __init__(
        self,
        commit_hash: str,
        branch: str,
        worktree_path: str,
        actual_head: str | None,
        reason: str,
    ) -> None:
        self.commit_hash = commit_hash
        self.branch = branch
        self.worktree_path = worktree_path
        self.actual_head = actual_head
        self.reason = reason
        super().__init__(
            f"Refusing to push commit {commit_hash} on branch {branch}: the "
            f"worktree {worktree_path} does not hold the pushed commit "
            f"(HEAD={actual_head or 'unresolved'}; {reason}). The dev-push "
            f"gate's subject is the pushed commit and it resolves that commit "
            f"from the push checkout, so pushing from a checkout holding a "
            f"different commit would gate the wrong tree. Recovery: check out "
            f"{commit_hash} in "
            f"{worktree_path} (or recreate the worktree from {branch}) and "
            f"re-run finish. The commit is local on branch {branch}."
        )


class AncestryAssertionError(RuntimeError):
    """Raised when finish's own commit did not reach the pushed dev tip.

    Carries the commit hash (full and as reported), the remote-tracking ref it
    was asserted against, and that ref's tip so the refusal message can name
    every hash involved (SA-0MVPZG7A0000N66U AC2). A finish that raises this
    must not report success and must not advance the work item to
    ``in_review``.
    """

    def __init__(
        self,
        commit_hash: str,
        resolved_commit: str | None,
        target_ref: str,
        target_tip: str | None,
        reason: str,
    ) -> None:
        self.commit_hash = commit_hash
        self.resolved_commit = resolved_commit
        self.target_ref = target_ref
        self.target_tip = target_tip
        self.reason = reason
        named = resolved_commit or commit_hash or "unknown"
        super().__init__(
            f"Ancestry assertion failed: commit {named} (reported as "
            f"{commit_hash or 'unknown'}) is not verifiably on {target_ref} "
            f"(tip {target_tip or 'unresolved'}): {reason}. finish did not "
            f"publish this commit to origin/dev, so it must not report "
            f"success. The worktree and the feature branch are retained for "
            f"recovery; the work item is not advanced to in_review. "
            f"Recovery: git log --oneline {target_ref}; then "
            f"git push origin HEAD:refs/heads/dev from the worktree (rebase "
            f"onto {target_ref} first if the push is rejected)."
        )


def _kill_process_group(pid: int) -> None:
    """Kill the entire process group rooted at *pid* (POSIX only).

    Used to clean up orphaned children of a timed-out ``git push`` — the
    pre-push hook spawns ``run_tests.py`` which may outlive the parent
    process if the ``git push`` is killed.

    Args:
        pid: PID of the process to kill (typically the ``git`` process).
    """
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # The process may have already exited — best effort.
        pass


#: The remote every framework push resolves through (SA-0MVX35ABT000I1PD AC1).
#:
#: Framework pushes ALWAYS use the configured remote **name** — never a path
#: or ``.`` form — so ``git push -u`` can never record a self-referential
#: upstream (``branch.<name>.remote = "."``) onto a long-lived branch and
#: silently disable ``git pull`` on a worktree machine (SA-0MVX1M6HH004YPX2).
PUSH_REMOTE_NAME = "origin"


def _assert_remote_name_only(remote: str) -> None:
    """Refuse a push remote that is not a plain remote name.

    The write path that records the self-referential upstream defect is a
    push whose remote is the **local path form** (``.`` — also reached via a
    path or ``file://`` URL). A framework push must therefore never accept
    anything except a plain configured remote name.

    Args:
        remote: The remote argument a framework push is about to use.

    Raises:
        RuntimeError: when *remote* is ``.``, a path or a URL — the shapes
            that would make ``git push -u`` write ``branch.<name>.remote = .``.
    """
    if remote == "." or "/" in remote or ":" in remote or "://" in remote:
        raise RuntimeError(
            f"Refusing a framework push to non-remote-name {remote!r}: use the "
            f"configured remote name {PUSH_REMOTE_NAME!r}. A path or '.' "
            "remote would make `git push -u` record a self-referential "
            "upstream (branch.<name>.remote = '.'), silently breaking "
            "`git pull` — see SA-0MVX1M6HH004YPX2."
        )


def _full_suite_cache_present(cwd: str) -> bool:
    """Whether a valid full-suite cache entry exists for *cwd*'s git state.

    Read-only: queries the test cache for each full-suite command the repo
    resolves and executes nothing. A missing suite, an unreadable command or
    an indeterminate git state is a miss — never a silent green. Fail-soft:
    evidence plumbing may never decide a push's outcome
    (SA-0MVUTXJM8006PQY4 AC3/AC4).
    """
    try:
        commands = _resolve_full_suite_commands(cwd) or []
    except Exception:  # noqa: BLE001 - evidence must never break a push
        return False
    for command in commands:
        try:
            if query_cached(command, cwd=cwd) is not None:
                return True
        except Exception as exc:  # noqa: BLE001 - a corrupt entry is a miss
            LOG.debug("Full-suite cache query failed for %r: %s", command, exc)
            continue
    return False


def _dev_push_gate_evidence(
    cwd: str, commit_hash: str | None, before_present: bool,
) -> dict[str, Any]:
    """Classify the dev-push gate's verdict for the pushed tree (evidence).

    ``.githooks/pre-push`` runs the full suite before the push contacts the
    remote, so a successful ``git push`` means the gate ran — but the captured
    output is only visible if it is logged, and whether the run was
    *executed* or served from the *cache* is only observable in the test
    cache. This resolves the gate's tree (root + ``HEAD``), the pushed sha and
    the classification from the read-only cache API:

    * a valid full-suite entry already present before the push → ``cached``;
    * no entry before but one after the push → ``executed``;
    * still none after a successful push → ``no-evidence`` (warned — never
      silently reported as green).

    Args:
        cwd: The push cwd (the pushed tree).
        commit_hash: The pushed commit (short or full).
        before_present: Whether a valid full-suite entry existed pre-push.

    Returns:
        Dict with ``root``, ``head``, ``pushed`` and ``full_suite``.
    """
    root = str(Path(cwd).resolve())
    head = _resolve_full_commit(cwd, "HEAD") or "unknown"
    pushed = (
        _resolve_full_commit(cwd, commit_hash) if commit_hash else None
    ) or (commit_hash or "unknown")
    after_present = _full_suite_cache_present(cwd)
    if before_present:
        classification = "cached"
    elif after_present:
        classification = "executed"
    else:
        classification = "no-evidence"
    evidence = {
        "root": root,
        "head": head,
        "pushed": pushed,
        "full_suite": classification,
    }
    line = (
        f"dev-push gate evidence: root={root} head={head} "
        f"pushed={pushed} full-suite={classification}"
    )
    if classification == "no-evidence":
        LOG.warning(
            "%s — no full-suite cache entry exists for the pushed tree, so "
            "the dev-push gate produced no evidence that it ran",
            line,
        )
    else:
        LOG.info("%s", line)
    return evidence


#: Verdicts of the read-only post-push verification step
#: (design A, SA-0MVXUSED6008J04R). ``green`` and ``not_applicable`` permit
#: the terminal ``completed``/``in_review`` transition; ``red`` and
#: ``missing`` refuse it (SA-0MVXUSJ2P009T97H AC4). Any unexpected value is
#: treated as ``missing`` — the refusal is fail-closed.
POST_PUSH_VERDICT_GREEN = "green"
POST_PUSH_VERDICT_RED = "red"
POST_PUSH_VERDICT_MISSING = "missing"
POST_PUSH_VERDICT_NOT_APPLICABLE = "not_applicable"


def _post_push_verification_commands(cwd: str) -> tuple[list[str], str]:
    """Resolve the full-suite commands the post-push verification checks.

    Resolution mirrors :func:`run_tests` (the test skill's
    ``full_suite_commands`` first, implement.py's legacy convention detection
    second) without executing anything, so the verification step can query
    the cache for exactly the command the dev-push gate ran.

    Args:
        cwd: The pushed-commit checkout (the worktree).

    Returns:
        ``(commands, reason)`` — the resolved commands and, when nothing was
        resolved, why (recorded as the ``not_applicable`` reason).
    """
    try:
        commands = _resolve_full_suite_commands(cwd)
    except Exception as exc:  # noqa: BLE001 - resolution must not break finish
        LOG.warning(
            "Post-push verification: full-suite resolution failed (%s)", exc
        )
        return [], f"full-suite resolution failed: {exc}"
    if commands is not None:
        if not commands:
            return [], "the repository declares no full-suite command"
        return list(commands), ""
    # The test skill is unavailable (partial install) or resolved no suite:
    # fall back to the legacy convention detection, limited to the two
    # commands with a canonical cache key. Other legacy tooling (a repo-local
    # runner script, Unity) has no canonical command to look up, so it is
    # reported as ``not_applicable`` with that reason rather than a silent
    # green (never invent a command that the gate may not have run).
    tooling = _detect_test_tooling(cwd)
    if tooling == "pytest":
        return [PYTEST_CMD], ""
    if tooling == "npm":
        return [NPM_TEST_CMD], ""
    if tooling is None:
        return [], "no full-suite command detected"
    return [], f"legacy tooling {tooling!r} has no canonical full-suite command"


def _resolve_post_push_verification(
    worktree_path: str, repo_root: str
) -> dict[str, Any]:
    """Read-only full-suite verdict for the just-pushed commit (design A).

    Part of :func:`phase_finish`'s finalisation window
    (SA-0MVXUSJ2P009T97H, design SA-0MVXUSED6008J04R): after a successful dev
    push the run resolves the full-suite verdict for the pushed tree from the
    test cache. It **never executes a suite** — the dev-push pre-push hook ran
    the full suite against this exact commit and cached the result.

    The pushed-commit checkout (``worktree_path``) is queried first, then the
    main checkout's cache (``repo_root``) as the documented fallback: the
    pre-push hook mirrors its entry into the main-checkout
    ``.worklog/cache``, while a worktree's own cache directory is separate.
    The cache lookup binds the verdict to the current git state (HEAD + dirty
    content), so an entry recorded for another tree can never be served.

    Verdict vocabulary:

    - ``green`` — every resolved full-suite command has a valid entry with
      ``exit_code == 0``. Transition permitted.
    - ``not_applicable`` — the repository resolves no full-suite command at
      all (or only legacy tooling with no canonical command). Transition
      permitted, recorded explicitly so it is never a silent green.
    - ``red`` — an entry exists but reports a non-zero exit. Transition
      refused.
    - ``missing`` — no valid entry: absent, expired, unkeyable or a bypassed
      pre-push gate. Transition refused.

    Args:
        worktree_path: The checkout that holds the pushed commit.
        repo_root: The main checkout (cache mirror location).

    Returns:
        A structured verdict dict (``verdict``, ``success``, ``executed``,
        ``commands``, ``entries``, ``reason``, ``checked_at``).
    """
    commands, reason = _post_push_verification_commands(worktree_path)
    verification: dict[str, Any] = {
        "verdict": POST_PUSH_VERDICT_NOT_APPLICABLE,
        "success": True,
        "executed": False,
        "commands": commands,
        "entries": [],
        "reason": reason,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "cache_roots": [worktree_path, repo_root],
    }
    if not commands:
        LOG.info("Post-push verification: not applicable (%s)", reason)
        return verification

    verdicts: list[str] = []
    for command in commands:
        entry: dict[str, Any] | None = None
        cached_at: str | None = None
        for cwd in (worktree_path, repo_root):
            try:
                entry = query_cached(command, cwd=cwd)
            except Exception as exc:  # noqa: BLE001 - a broken cache is a miss
                LOG.warning(
                    "Post-push verification: cache query failed for %s in "
                    "%s (%s)",
                    command,
                    cwd,
                    exc,
                )
                entry = None
            if entry is not None:
                cached_at = cwd
                break
        if entry is None:
            verdicts.append(POST_PUSH_VERDICT_MISSING)
            verification["entries"].append(
                {
                    "command": command,
                    "verdict": POST_PUSH_VERDICT_MISSING,
                    "cache_root": None,
                }
            )
            continue
        exit_code = entry.get("exit_code")
        try:
            ok = int(exit_code) == 0
        except (TypeError, ValueError):
            ok = False
        verdict = (
            POST_PUSH_VERDICT_GREEN if ok else POST_PUSH_VERDICT_RED
        )
        verdicts.append(verdict)
        verification["entries"].append(
            {
                "command": command,
                "verdict": verdict,
                "cache_root": cached_at,
                "exit_code": exit_code,
                "completed_at": entry.get("completed_at"),
                "scope": entry.get("scope"),
                "git_state": entry.get("git_state"),
                "fingerprint_kind": entry.get("fingerprint_kind"),
                "stdout": (entry.get("stdout") or "")[:4000],
                "stderr": (entry.get("stderr") or "")[:4000],
            }
        )

    # Red wins over missing (a real failure is more actionable than a gap),
    # and green requires every command to be green.
    if POST_PUSH_VERDICT_RED in verdicts:
        verdict = POST_PUSH_VERDICT_RED
    elif POST_PUSH_VERDICT_MISSING in verdicts:
        verdict = POST_PUSH_VERDICT_MISSING
    else:
        verdict = POST_PUSH_VERDICT_GREEN
    verification["verdict"] = verdict
    verification["success"] = verdict == POST_PUSH_VERDICT_GREEN
    if verdict == POST_PUSH_VERDICT_GREEN:
        LOG.info(
            "Post-push verification: green (full-suite cache entry for the "
            "pushed tree)"
        )
    else:
        LOG.warning(
            "Post-push verification: %s — refusing the terminal transition",
            verdict,
        )
    return verification


def _post_push_failure_test_name(verification: dict[str, Any]) -> str:
    """Name the failure a red/missing verification reports to triage.

    A red entry carries the failing suite's output, so the first ``FAILED``/
    ``✕`` line names the real test; a missing entry has no failing test and is
    named after the verification itself.
    """
    for entry in verification.get("entries") or []:
        if entry.get("verdict") != POST_PUSH_VERDICT_RED:
            continue
        output = f"{entry.get('stdout') or ''}\n{entry.get('stderr') or ''}"
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("FAILED "):
                return line[len("FAILED ") :].split()[0]
            if line.startswith("✕ "):
                return line[2:].strip()
        return f"post-push full-suite verification ({entry.get('command')})"
    return "post-push full-suite verification: no valid full-suite cache entry"


def _route_post_push_failure_to_triage(
    verification: dict[str, Any], commit_hash: str, work_item_id: str
) -> dict[str, Any]:
    """File a red/missing post-push verification through the triage helper.

    Fail-soft by design: a triage hiccup must never mask the verification
    refusal that is being reported. The helper is the skill's canonical
    ``check_or_create.py``, so an existing matching critical ``test-failure``
    item is enhanced instead of duplicated.
    """
    excerpt_parts: list[str] = []
    stack_trace = ""
    for entry in verification.get("entries") or []:
        if entry.get("verdict") == POST_PUSH_VERDICT_MISSING:
            excerpt_parts.append(
                f"No valid full-suite cache entry for: {entry.get('command')}"
            )
            continue
        excerpt_parts.append(f"$ {entry.get('command')}")
        excerpt_parts.append((entry.get("stdout") or "")[:1500])
        if entry.get("stderr"):
            stack_trace = (entry.get("stderr") or "")[:1500]
    payload = {
        "test_name": _post_push_failure_test_name(verification),
        "stdout_excerpt": "\n".join(excerpt_parts)[:3000],
        "stack_trace": stack_trace,
        "commit_hash": commit_hash,
        "parent_work_item_id": work_item_id,
    }
    helper = _SKILLS_ROOT / "triage" / "scripts" / "check_or_create.py"
    if not helper.exists():
        LOG.warning(
            "Post-push verification triage helper not found at %s", helper
        )
        return {"success": False, "reason": "helper_not_found"}
    try:
        proc = subprocess.run(
            [sys.executable, str(helper), json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.warning("Post-push verification triage failed: %s", exc)
        return {"success": False, "reason": f"triage_failed: {exc}"}
    result: dict[str, Any] = {
        "success": proc.returncode == 0,
        "returncode": proc.returncode,
    }
    try:
        result["report"] = json.loads(proc.stdout.strip() or "{}")
    except (json.JSONDecodeError, ValueError):
        result["stdout"] = proc.stdout[-2000:]
    if proc.returncode != 0:
        result["stderr"] = proc.stderr[-2000:]
        LOG.warning(
            "Post-push verification triage helper exited %d: %s",
            proc.returncode,
            proc.stderr[-500:],
        )
    return result


def _post_push_verification_refusal(
    verification: dict[str, Any],
    work_item_id: str,
    commit_hash: str,
    branch: str,
    worktree_path: str,
) -> tuple[str, str]:
    """Build the refusal message and the work-item comment (AC4).

    The refused state is named exactly: the pushed commit, the branch, the
    retained worktree, the verdict and the cache state checked. Nothing was
    reverted — the commit stays on ``dev`` and on its feature branch.

    Returns:
        ``(message, comment)`` — the RuntimeError text and the comment body.
    """
    verdict = verification.get("verdict")
    reason = verification.get("reason") or ""
    lines = [
        f"Finish refused: post-push verification is {verdict}.",
        f"- Commit: {commit_hash} (pushed to dev, not reverted)",
        f"- Branch: {branch} (retained)",
        f"- Worktree: {worktree_path} (retained)",
        "- Full-suite commands checked:",
    ]
    for entry in verification.get("entries") or []:
        detail = entry.get("verdict")
        if entry.get("exit_code") is not None:
            detail += f", exit_code={entry.get('exit_code')}"
        if entry.get("cache_root"):
            detail += f", cache_root={entry.get('cache_root')}"
        lines.append(f"  - {entry.get('command')}: {detail}")
    if reason:
        lines.append(f"- Reason: {reason}")
    if verdict == POST_PUSH_VERDICT_RED:
        lines.append(
            "The cached full-suite verdict for the pushed tree is red, so the "
            "item is not marked complete. The failure has been routed to "
            "triage."
        )
    else:
        lines.append(
            "No valid full-suite cache entry exists for the pushed tree "
            "(absent, expired, unkeyable, or the pre-push gate was bypassed), "
            "so a green verdict cannot be proven."
        )
    lines.extend(
        [
            (
                "The work item was released to `open` with no assignee; the "
                "pushed commit is neither reverted nor force-removed."
            ),
            (
                "Remediation: run the full suite through the test skill, then "
                "re-run finish (idempotent — the no-diff path converges):"
            ),
            "  python3 skill/test/scripts/run_tests.py --scope full",
            (
                f"  python3 skill/implement/scripts/implement.py finish "
                f"{work_item_id}"
            ),
        ]
    )
    comment = "\n".join(lines)
    message = (
        f"Post-push verification refused the terminal transition for "
        f"{work_item_id}: verdict={verdict}, commit={commit_hash}. The commit "
        f"stays on dev and on branch {branch}; the worktree is retained. "
        f"Run the full suite through the test skill and re-run finish."
    )
    return message, comment


PUSH_TRANSPORT_RETRY_PATTERNS: tuple[str, ...] = (
    "closed by remote host",
    "connection reset by peer",
    "remote end hung up unexpectedly",
    "kex_exchange_identification",
)
#: Bounded retry policy (AC4): the initial attempt plus two retries with
#: jittered exponential backoff. A retry re-runs the identical argv/cwd/env, so
#: the pre-push gate runs again and serves its cached full-suite entry.
PUSH_RETRY_MAX_ATTEMPTS = 3
PUSH_RETRY_BASE_SECONDS = 2.0
PUSH_RETRY_CAP_SECONDS = 30.0

_GATE_REASON_LINE_RE = re.compile(r"^gate_reason=([a-z_]+)\s*$", re.MULTILINE)

#: Reason reported when the repo-local pre-push **pushed-commit guard** refuses
#: (§3a, LP-0MVSJUM3O003C0H9). The guard exits *before* the suite runs, so it
#: emits no ``gate_reason=`` line; the refusal is recognised from its stable
#: diagnostic instead, which makes it distinguishable from a generic push
#: failure (SA-0MW3H07A8004Q54C AC3).
PUSHED_COMMIT_GUARD_REASON = "refused_by_pushed_commit_guard"
_PUSHED_COMMIT_GUARD_MARKER = "pre-push gate refuses this push"
_PUSHED_COMMIT_GUARD_PUSHED_RE = re.compile(
    r"pushed commit[^\n:]*:\s*([0-9a-f]{6,40})"
)
_PUSHED_COMMIT_GUARD_HEAD_RE = re.compile(
    r"checked-out HEAD:\s*([0-9a-f]{6,40})"
)
_PUSHED_COMMIT_GUARD_TREE_RE = re.compile(r"gate tree:\s*(\S+)")


class PushGateFailureError(RuntimeError):
    """A dev push refused by the pre-push gate, carrying its reason code.

    Carries the hook's stable ``gate_reason=<code>`` so ``phase_finish`` can
    report *why* the push failed (slot contention / timeout / killed / red
    suite) instead of a generic "git push to dev failed"
    (SA-0MVSJX5HS002DJHI AC2).
    """

    def __init__(
        self,
        gate_reason: str,
        stderr_tail: str,
        commit_hash: str,
        branch: str,
    ) -> None:
        self.gate_reason = gate_reason
        self.stderr_tail = stderr_tail
        self.commit_hash = commit_hash
        self.branch = branch
        super().__init__(
            f"Push to dev refused by the gate ({gate_reason}): {stderr_tail}. "
            f"Commit {commit_hash} on branch {branch} is local."
        )


def _is_retryable_push_transport(stderr: str) -> bool:
    """Whether *stderr* names a transient transport close (retryable).

    Only the recognised transport patterns retry. Auth failures, branch-policy
    refusals and non-fast-forward errors must fail as they do today — masking
    them behind a retry would turn a real error into a slow one.
    """
    lower = (stderr or "").lower()
    return any(pattern in lower for pattern in PUSH_TRANSPORT_RETRY_PATTERNS)


def _push_transport_backoff(
    attempt: int, rng: random.Random | None = None
) -> float:
    """Jittered exponential backoff (seconds) for the *attempt*-th retry.

    *attempt* is 1-based (1 = the delay after the first failed push). The base
    doubles per retry and is capped, with +/-25% jitter so concurrent pushers do
    not retry in lockstep.
    """
    rng = rng if rng is not None else random
    base = PUSH_RETRY_BASE_SECONDS * (2 ** (max(1, attempt) - 1))
    capped = min(base, PUSH_RETRY_CAP_SECONDS)
    return capped * (0.75 + rng.random() * 0.5)


def _push_retry_sleep(seconds: float) -> None:
    """Sleep seam for the transport backoff (monkeypatched in tests)."""
    time.sleep(seconds)


def _parse_gate_reason(stderr: str) -> str | None:
    """The last stable ``gate_reason=<code>`` line the hook printed, if any."""
    matches = _GATE_REASON_LINE_RE.findall(stderr or "")
    return matches[-1] if matches else None


def _parse_pushed_commit_guard_refusal(stderr: str) -> dict[str, str] | None:
    """Return the pushed-commit guard's refusal details, or ``None``.

    §3a of ``.githooks/pre-push`` refuses a dev/main push *before* the suite
    runs, so it prints no ``gate_reason=`` line. Its refusal is recognised from
    the stable marker and the ``pushed commit`` / ``checked-out HEAD`` /
    ``gate tree`` values it names — the same three the operator needs to act
    (SA-0MW3H07A8004Q54C AC3). Missing fields are omitted, never fabricated.
    """
    text = stderr or ""
    if _PUSHED_COMMIT_GUARD_MARKER not in text.lower():
        return None
    details: dict[str, str] = {}
    pushed = _PUSHED_COMMIT_GUARD_PUSHED_RE.search(text)
    if pushed:
        details["pushed_sha"] = pushed.group(1)
    head = _PUSHED_COMMIT_GUARD_HEAD_RE.search(text)
    if head:
        details["gate_head"] = head.group(1)
    tree = _PUSHED_COMMIT_GUARD_TREE_RE.search(text)
    if tree:
        details["gate_tree"] = tree.group(1)
    return details


def _stderr_tail(text: str, limit: int = 20) -> str:
    """The last *limit* lines of a push/gate stderr (bounded for comments)."""
    lines = (text or "").strip().splitlines()
    return "\n".join(lines[-limit:])


def _push_gate_remediation(gate_reason: str | None) -> str:
    """The operator remedy for a dev-push gate reason (never empty)."""
    if gate_reason == PUSHED_COMMIT_GUARD_REASON:
        # AC3: the recovery is the guard's own documented alternative — push
        # from a checkout that *is* the pushed commit.
        return (
            "The pre-push pushed-commit guard refused: the gate tree was not "
            "the pushed commit, or was dirty at it. Push from the worktree "
            "that holds the pushed commit (git push origin "
            "HEAD:refs/heads/dev from that worktree), or bring the main "
            "checkout to the pushed commit and re-run finish."
        )
    if gate_reason and _GATE_REMEDIATION.get(gate_reason):
        return str(_GATE_REMEDIATION[gate_reason])
    return (
        "Read the gate output above, resolve the condition, then re-run "
        "finish (idempotent — a no-diff push converges)."
    )


def _dev_push_gate_failure_comment(
    gate_reason: str,
    stderr_tail: str,
    commit_hash: str,
    branch: str,
    worktree_path: str,
) -> str:
    """Build the work-item comment for a dev push refused by the gate (AC2)."""
    lines = [
        f"Dev push failed (gate reason: {gate_reason}).",
        _push_gate_remediation(gate_reason),
        "",
        f"Commit: {commit_hash}",
        f"Branch: {branch}",
        f"Worktree: {worktree_path}",
        (
            "Local dev was restored to origin/dev (the commit stays on its "
            "feature branch)."
        ),
    ]
    if stderr_tail:
        lines.extend(["", "Gate output (tail):", stderr_tail])
    return "\n".join(lines)


def git_push_to_dev(
    cwd: str,
    branch: str,
    commit_hash: str | None = None,
    timeout: int | None = None,
    evidence_out: dict[str, Any] | None = None,
) -> bool:
    """Push the current branch into the dev branch on origin.

    The push timeout is resolved from ``.pi/test-config.json``
    (``timeoutPerCommand``) and defaults to 600 s.  On timeout the
    pre-push hook's child processes are terminated via process-group
    kill (AC2).  If the timeout expires a ``PushTimeoutError`` is raised
    carrying the commit hash and branch so the caller can post a
    manual-push comment and leave recoverable state (AC3).

    A **transient transport close** (``Connection ... closed by remote
    host``, ``Connection reset by peer``, ``remote end hung up
    unexpectedly``, ``kex_exchange_identification``) is retried up to
    ``PUSH_RETRY_MAX_ATTEMPTS`` attempts with jittered exponential
    backoff from the *same cwd*, so the gate runs again and serves its
    cached full-suite entry (SA-0MVSJX5HS002DJHI AC4). Auth, branch-policy
    and non-fast-forward failures are never retried.

    Args:
        cwd: The checkout that holds the pushed commit — the worktree. The
            pre-push hook resolves its project root with
            ``git rev-parse --show-toplevel`` in this directory, so the
            dev-push full-suite gate measures the pushed tree
            (SA-0MVTFMB7K002Y1F3). Never the main checkout unless the commit
            is checked out there.
        branch: Local branch name to push.
        commit_hash: The commit hash being pushed (used in recovery).
        timeout: Override timeout in seconds (defaults to resolved config).
        evidence_out: Optional dict populated with the gate's ``root``,
            ``head``, ``pushed`` and ``full_suite`` classification on success
            (SA-0MVUTXJM8006PQY4 AC5), or with ``success: False``,
            ``gate_reason`` and ``stderr_tail`` on failure
            (SA-0MVSJX5HS002DJHI — additive; the success keys are unchanged).

    Returns:
        True if the push succeeded.

    Raises:
        ``PushTimeoutError``: push timed out — carries commit_hash/branch
            so the caller can produce a recovery comment.
        ``PushNonFastForwardError``: dev advanced concurrently — the caller
            rebases and retries.
    """
    if timeout is None:
        timeout = _resolve_test_timeout(cwd)

    # Remote-name-only rule (SA-0MVX35ABT000I1PD AC1): the push must always
    # resolve through the configured remote name, never a path/'.' form —
    # otherwise `git push -u` at a call site could record
    # branch.<name>.remote = '.' and silently disable `git pull` on a
    # worktree machine (SA-0MVX1M6HH004YPX2). The guard runs on every push;
    # the constant pins the name that carries dev-push integration.
    _assert_remote_name_only(PUSH_REMOTE_NAME)

    # Snapshot the full-suite cache before the FIRST push attempt so the gate's
    # executed-vs-cached verdict is observable afterwards (AC3). Read-only and
    # fail-soft: it never decides the push. Taken once so a transport retry is
    # never misreported as "cached" merely because the first attempt executed.
    before_present = _full_suite_cache_present(cwd)

    for attempt in range(1, PUSH_RETRY_MAX_ATTEMPTS + 1):
        # Spawn the push in its own process group so that a timeout kills the
        # entire tree of children (e.g. the pre-push hook's ``run_tests.py``).
        try:
            # Augment the push child env with the resolved disk-backed TMPDIR
            # so the pre-push hook's ``run_tests.py`` inherits it (fail-open:
            # a resolution failure leaves the ambient env unchanged). Neither
            # this export nor the resolution mutates the parent shell env
            # (SA-0MW3ENQY0001RBSW AC1/AC3).
            child_env = dict(os.environ)
            _finish_tmpdir = _resolved_finish_tmpdir()
            if _finish_tmpdir is not None:
                child_env["TMPDIR"] = _finish_tmpdir
            proc = subprocess.Popen(
                ["git", "push", PUSH_REMOTE_NAME, f"{branch}:refs/heads/dev"],
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,  # new process group
                env=child_env,
            )
            try:
                _stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Kill the entire process group so orphaned hook children don't
                # survive and deadlock subsequent push attempts.
                _kill_process_group(proc.pid)
                proc.wait()
                LOG.error(
                    "git push to dev timed out after %d s (pre-push hook still "
                    "running).",
                    timeout,
                )
                # Raise with recoverable-state info (AC3). A timeout is a bound,
                # not a transport close: never retried here.
                raise PushTimeoutError(
                    commit_hash or "unknown", branch, timeout,
                ) from None
            stderr_str = stderr.decode(errors="replace").strip()
            if proc.returncode == 0:
                # Success: surface the pre-push hook's output (AC3). It was
                # previously captured and dropped, so "the gate ran" and "the
                # gate never ran" were indistinguishable. Then classify
                # executed-vs-cached from the read-only test cache for the
                # pushed tree.
                if stderr_str:
                    for hook_line in stderr_str.splitlines():
                        LOG.info("git push to dev hook: %s", hook_line)
                evidence = _dev_push_gate_evidence(cwd, commit_hash, before_present)
                if evidence_out is not None:
                    evidence_out.clear()
                    evidence_out.update(evidence)
                return True

            # Failure: detect non-fast-forward errors (dev advanced
            # concurrently). AC1/AC2: raise a recoverable exception so the
            # caller can rebase and retry instead of failing with information
            # loss. Remotes word this differently: SSH/HTTPS report
            # "non-fast-forward", while local/file remotes report "fetch
            # first" / "remote contains work that you do not have locally".
            lower = stderr_str.lower()
            if (
                "non-fast-forward" in lower
                or "fetch first" in lower
                or "remote contains work" in lower
            ):
                rebase_cmd = (
                    f"git rebase origin/dev && git push origin "
                    f"{branch}:refs/heads/dev"
                )
                raise PushNonFastForwardError(
                    commit_hash or "unknown", branch, stderr_str, rebase_cmd,
                )

            # Transient transport close: retry from the SAME cwd (the worktree
            # that holds the pushed commit), so the gate re-runs against the
            # identical tree and serves its cached entry.
            if (
                _is_retryable_push_transport(stderr_str)
                and attempt < PUSH_RETRY_MAX_ATTEMPTS
            ):
                delay = _push_transport_backoff(attempt)
                LOG.warning(
                    "git push to dev attempt %d/%d failed with a transient "
                    "transport error; retrying in %.1fs from the same cwd %s. "
                    "stderr: %s",
                    attempt,
                    PUSH_RETRY_MAX_ATTEMPTS,
                    delay,
                    cwd,
                    _stderr_tail(stderr_str),
                )
                _push_retry_sleep(delay)
                continue

            # The pushed-commit guard (§3a) refuses before the suite runs and
            # prints no ``gate_reason=`` line: recognise its stable diagnostic
            # so a guard refusal is reported distinctly, with the pushed sha /
            # gate HEAD / gate tree it names (SA-0MW3H07A8004Q54C AC3).
            pushed_commit_guard = _parse_pushed_commit_guard_refusal(stderr_str)
            gate_reason = (
                PUSHED_COMMIT_GUARD_REASON
                if pushed_commit_guard is not None
                else _parse_gate_reason(stderr_str)
            )
            if evidence_out is not None:
                evidence_out.clear()
                failure: dict[str, Any] = {
                    "success": False,
                    "gate_reason": gate_reason,
                    "stderr_tail": _stderr_tail(stderr_str),
                }
                if pushed_commit_guard is not None:
                    failure["pushed_commit_guard"] = pushed_commit_guard
                evidence_out.update(failure)
            LOG.error(
                "git push to dev failed (attempt %d/%d, gate_reason=%s): %s",
                attempt,
                PUSH_RETRY_MAX_ATTEMPTS,
                gate_reason or "none",
                stderr_str,
            )
            return False
        except Exception as exc:
            if isinstance(
                exc,
                (
                    PushTimeoutError,
                    PushNonFastForwardError,
                    PushGateFailureError,
                ),
            ):
                raise
            LOG.error("git push to dev error: %s", exc)
            return False

    return False


def git_get_commit_hash(cwd: str) -> str:
    """Get the current HEAD commit hash.

    Args:
        cwd: Working directory.

    Returns:
        Short commit hash string.
    """
    result = run_cmd(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=cwd,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return "unknown"


def _get_repo_root(cwd: str | None = None) -> str | None:
    """Get the absolute path to the MAIN git repository root.

    ``git rev-parse --show-toplevel`` resolves to the root of the *current*
    working tree. Inside a linked worktree (``.git`` is a file, not a
    directory) that is the worktree root, not the main checkout. Callers
    (the ``_remove_worktree`` safety gate, node_modules symlinking, repo
    restore/push) need the main checkout root, so resolve it via
    ``git rev-parse --git-common-dir`` (whose parent is the main working
    tree root) when *cwd* is inside a linked worktree.

    Args:
        cwd: Optional working directory (defaults to current directory).

    Returns:
        Absolute main repo root path, or None if not in a git repo.
    """
    result = run_cmd(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        check=False,
        timeout=30,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    toplevel = Path(result.stdout.strip()).resolve()

    # The main working tree has .git as a DIRECTORY; a linked worktree has
    # it as a FILE pointing at the real git directory.
    if (toplevel / ".git").is_dir():
        return toplevel.as_posix()

    # cwd is inside a linked worktree: derive the main repo root from the
    # common git dir (its parent is the main working tree root).
    common = run_cmd(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=cwd,
        check=False,
        timeout=30,
    )
    if common.returncode == 0 and common.stdout.strip():
        common_dir = Path(common.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = toplevel / common_dir
        main_root = common_dir.resolve().parent
        git_marker = main_root / ".git"
        if git_marker.is_dir() or git_marker.is_file():
            return main_root.as_posix()
    return toplevel.as_posix()


def _is_working_tree_root(root: str | None) -> bool:
    """True when *root* is an existing directory holding a ``.git`` marker.

    Used to validate a **derived** main checkout root before it is trusted:
    every legitimate answer (:func:`_get_repo_root`) is a working-tree root,
    so a value without the marker means derivation effectively failed and the
    documented fallback must apply instead. Returning such a path would make
    finish snapshot, fast-forward and lock a directory that is not a checkout
    at all (SA-0MVXVC03O0034A7V).

    Args:
        root: Candidate root path (``None``/empty means "not derivable").
    """
    if not root:
        return False
    try:
        candidate = Path(root)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return False
    marker = candidate / ".git"
    return candidate.is_dir() and (marker.is_dir() or marker.is_file())


def _resolve_main_repo_root(
    worktree_path: str | None = None,
    state: ImplementState | None = None,
) -> str:
    """Resolve the MAIN checkout root for a worktree-scoped phase.

    The main root is **derived**, never trusted from state alone. A state
    file's ``repo_root`` is accepted only when it resolves to the same main
    checkout that :func:`_get_repo_root` derives from *worktree_path* (via
    ``git rev-parse --git-common-dir``, whose parent is the main working-tree
    root). Two defects are closed by that requirement
    (SA-0MVXVC03O0034A7V):

    * a **stale** value written by ``phase_start`` when it resumed from inside
      a linked worktree — the worktree itself, or a path inside it — which
      made ``phase_finish`` refuse to remove "the repository root" and
      ``phase_abort`` restore the wrong checkout (MS-0MVM7NJF70037YU4);
    * a value naming a **sibling checkout** (another repository), which made
      finish snapshot, fast-forward and push that foreign repository — the
      2026-10-31 AI_Hell cross-checkout attribution incident.

    Both are rejected with a WARNING naming the rejected and the derived root,
    and the **derived** root is used. Derivation counts as impossible either
    when ``git`` cannot answer for the worktree **or** when the answer is not a
    real working-tree root (:func:`_is_working_tree_root`); then the documented
    fallback applies — the stated root, else discovery from the current
    directory, which is the last resort only (a finish run's cwd may be an
    unrelated checkout) — and it is logged too, so a silently trusted value
    cannot exist. The rule is documented for operators in
    ``docs/dev/implement-skill-reference.md`` § Finish main-checkout guard →
    Fail-closed `repo_root` resolution.

    Args:
        worktree_path: Absolute path to the phase's worktree, if known.
        state: The implement state read for this worktree, if available.

    Returns:
        Absolute main checkout root. Whenever the repository owning
        *worktree_path* is derivable this is never a path outside it (the
        worktree itself and paths inside it are rejected too, unless the
        worktree *is* the main checkout — the callers' removal safety gates
        then refuse on their own terms).
    """
    worktree = Path(worktree_path).resolve() if worktree_path else None
    stated = (state.repo_root if state else "") or ""

    # Derive from the worktree only while it exists: an already-removed
    # worktree makes derivation impossible (``git`` cannot run in it) rather
    # than an error, which keeps ``cleanup``/``abort`` idempotent.
    derived: str | None = None
    if worktree is not None and worktree.is_dir():
        derived = _get_repo_root(str(worktree))
        if derived and not _is_working_tree_root(derived):
            LOG.warning(
                "Ignoring derived main checkout root %s for worktree %s: it "
                "is not a git working tree, so the derivation is treated as "
                "impossible.",
                derived,
                worktree,
            )
            derived = None

    if derived:
        if stated:
            resolved_stated = Path(stated).resolve()
            if resolved_stated != Path(derived).resolve():
                inside_worktree = bool(
                    worktree is not None
                    and (
                        resolved_stated == worktree
                        or worktree in resolved_stated.parents
                    )
                )
                if inside_worktree:
                    LOG.warning(
                        "Ignoring stale state repo_root=%s (it is the "
                        "worktree or inside it); using the main checkout "
                        "root %s instead.",
                        stated,
                        derived,
                    )
                else:
                    LOG.warning(
                        "Rejecting state repo_root=%s: it is not the main "
                        "checkout that owns worktree %s (derived main "
                        "checkout root %s). Using the derived root — finish "
                        "never acts on another checkout.",
                        stated,
                        worktree_path,
                        derived,
                    )
        return derived

    # Derivation impossible: the documented fallback is the only answer left,
    # and it is surfaced rather than trusted. Discovery from the current
    # directory is the last resort — never preferred over a stated root, since
    # the cwd of a finish run may be an unrelated checkout entirely.
    fallback = (
        str(Path(stated).resolve())
        if stated
        else (_get_repo_root() or str(Path.cwd().resolve()))
    )
    LOG.warning(
        "Cannot derive the main checkout root for worktree %s from git; "
        "falling back to %s.",
        worktree_path or Path.cwd(),
        fallback,
    )
    return fallback


#: Workspace package consumed by path whose resolution a worktree must own
#: (the reference case: a package that the main checkout's dependency tree
#: links back into its own ``packages/`` tree).
SHARED_WORKSPACE_PACKAGE_NAME = "@worklog/shared"

#: Workspace-relative location of the shared package above.
SHARED_WORKSPACE_PACKAGE_DIR = Path("packages") / "shared"


def _read_package_json(package_dir: Path) -> dict[str, Any]:
    """Read ``<package_dir>/package.json`` as a dict ({} when unreadable)."""
    try:
        with open(package_dir / "package.json", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _workspace_glob_patterns(checkout_root: Path) -> list[str]:
    """Return the checkout's npm ``workspaces`` glob patterns.

    Accepts both the array form (``"workspaces": ["packages/*"]``) and the
    object form (``{"packages": [...]}``). Anything else is treated as "no
    workspaces", which keeps the step a no-op for plain repositories.
    """
    workspaces = _read_package_json(checkout_root).get("workspaces")
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages")
    if isinstance(workspaces, str):
        workspaces = [workspaces]
    if not isinstance(workspaces, list):
        return []
    return [pattern for pattern in workspaces if isinstance(pattern, str) and pattern]


def _workspace_packages(checkout_root: Path) -> dict[str, Path]:
    """Map workspace package name -> package directory inside *checkout_root*.

    Discovery is workspace-aware (the ``workspaces`` globs of the checkout's
    own ``package.json``), never hard-coded to one package name. Patterns that
    escape the checkout (``../*``) and symlinked package directories are
    ignored so the farm can only point at real in-tree sources.
    """
    root = checkout_root.resolve()
    packages: dict[str, Path] = {}
    for pattern in _workspace_glob_patterns(checkout_root):
        try:
            candidates = sorted(checkout_root.glob(pattern))
        except (OSError, ValueError) as exc:  # pragma: no cover - defensive
            LOG.warning("Ignoring invalid workspaces pattern %r: %s", pattern, exc)
            continue
        for candidate in candidates:
            if not candidate.is_dir() or candidate.is_symlink():
                continue
            try:
                resolved = candidate.resolve()
            except OSError:  # pragma: no cover - defensive
                continue
            if resolved != root and root not in resolved.parents:
                continue
            name = _read_package_json(candidate).get("name")
            if isinstance(name, str) and name and name not in packages:
                packages[name] = resolved
    return packages


def _workspace_link_overrides(
    worktree_path: Path, main_node_modules: Path
) -> dict[str, Path]:
    """Return ``node_modules``-relative package paths that must point at the worktree.

    A workspace package is only overridden when the main checkout actually
    materialises it in ``node_modules`` (a package consumed by path); repos
    without such a package therefore keep the whole-directory symlink
    degradation path.
    """
    overrides: dict[str, Path] = {}
    for name, package_dir in _workspace_packages(worktree_path).items():
        rel = Path(*name.split("/"))
        if os.path.lexists(main_node_modules / rel):
            overrides[name] = package_dir
    return overrides


def _create_farm_link(target: Path, link_path: Path, *, is_dir: bool) -> None:
    """Create *link_path* pointing at *target* (junction fallback on Windows)."""
    try:
        os.symlink(str(target), str(link_path), target_is_directory=is_dir)
        return
    except (OSError, NotImplementedError) as exc:
        if not (os.name == "nt" and is_dir):
            raise
        # ContextHub ``packages/herdr/scripts/link-shared.mjs`` precedent:
        # directory symlinks need privileges on Windows, junctions do not.
        junction = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if junction.returncode != 0:
            detail = junction.stderr.strip() or junction.stdout.strip()
            raise OSError(f"symlink failed ({exc}); junction failed: {detail}") from exc


def _remove_farm_entry(entry: Path) -> None:
    """Remove one farm entry (link, file or directory); never fatal."""
    try:
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        elif entry.is_dir():
            shutil.rmtree(entry)
    except OSError as exc:  # pragma: no cover - defensive
        LOG.warning("Failed to remove stale link-farm entry %s: %s", entry, exc)


def _ensure_farm_link(link_path: Path, target: Path) -> bool:
    """Make *link_path* a link to *target*, replacing a wrong/stale entry."""
    target_str = str(target)
    if link_path.is_symlink():
        try:
            if os.readlink(link_path) == target_str:
                return True
        except OSError:  # pragma: no cover - defensive
            pass
        _remove_farm_entry(link_path)
    elif os.path.lexists(link_path):
        # The farm is fully managed (``npm install`` in a worktree is
        # forbidden), so a stale real entry is replaced rather than shadowing
        # the main checkout's dependency.
        _remove_farm_entry(link_path)
    try:
        _create_farm_link(target, link_path, is_dir=target.is_dir())
    except (OSError, NotImplementedError) as exc:
        LOG.warning("Failed to link %s -> %s: %s", link_path, target, exc)
        return False
    return True


def _is_link_farm(wt_node_modules: Path) -> bool:
    """True when *wt_node_modules* looks like a farm created by this helper.

    A farm is a real directory whose root entries are links, except scope
    directories (``@scope``), which are real directories holding only links.
    A real ``npm install`` (package directories) is therefore never mistaken
    for a farm and is left untouched.
    """
    if wt_node_modules.is_symlink() or not wt_node_modules.is_dir():
        return False
    try:
        entries = list(os.listdir(wt_node_modules))
    except OSError:  # pragma: no cover - defensive
        return False
    for name in entries:
        entry = wt_node_modules / name
        if entry.is_symlink():
            continue
        if name.startswith("@") and entry.is_dir():
            try:
                children = list(os.listdir(entry))
            except OSError:  # pragma: no cover - defensive
                return False
            if all((entry / child).is_symlink() for child in children):
                continue
        return False
    return True


def _reconcile_scope(
    wt_scope: Path,
    main_scope: Path,
    overrides: dict[str, Path],
    scope_name: str,
) -> bool:
    """Materialise one scope directory of per-package links; return success."""
    if os.path.lexists(wt_scope) and (
        wt_scope.is_symlink() or not wt_scope.is_dir()
    ):
        _remove_farm_entry(wt_scope)
    if not os.path.lexists(wt_scope):
        try:
            wt_scope.mkdir()
        except OSError as exc:
            LOG.warning("Failed to create scope directory %s: %s", wt_scope, exc)
            return False
    try:
        main_children = sorted(os.listdir(main_scope))
    except OSError as exc:  # pragma: no cover - defensive
        LOG.warning("Failed to read scope %s: %s", main_scope, exc)
        return False

    ok = True
    for child in main_children:
        package_name = f"{scope_name}/{child}"
        target = overrides.get(package_name, main_scope / child)
        if not _ensure_farm_link(wt_scope / child, target):
            ok = False
    try:
        existing = list(os.listdir(wt_scope))
    except OSError:  # pragma: no cover - defensive
        existing = []
    for child in existing:
        if child not in main_children:
            _remove_farm_entry(wt_scope / child)
    return ok


def _reconcile_link_farm(
    wt_node_modules: Path,
    main_node_modules: Path,
    overrides: dict[str, Path],
) -> bool:
    """Bring an existing farm in line with the main checkout's entry set.

    New dependencies are linked in, removed ones are pruned, every workspace
    override keeps pointing at the worktree's own package, and no dependency
    tree is ever descended into (one level, plus one scope level).
    """
    try:
        main_entries = sorted(os.listdir(main_node_modules))
    except OSError as exc:  # pragma: no cover - defensive
        LOG.warning("Failed to read %s: %s", main_node_modules, exc)
        return False

    ok = True
    for name in main_entries:
        main_entry = main_node_modules / name
        is_scope = (
            name.startswith("@") and main_entry.is_dir() and not main_entry.is_symlink()
        )
        if is_scope:
            if not _reconcile_scope(
                wt_node_modules / name, main_entry, overrides, name
            ):
                ok = False
        else:
            target = overrides.get(name, main_entry)
            if not _ensure_farm_link(wt_node_modules / name, target):
                ok = False

    try:
        existing = list(os.listdir(wt_node_modules))
    except OSError:  # pragma: no cover - defensive
        existing = []
    for name in existing:
        if name not in main_entries:
            _remove_farm_entry(wt_node_modules / name)
    return ok


def _materialise_link_farm(
    wt_node_modules: Path,
    main_node_modules: Path,
    overrides: dict[str, Path],
) -> bool:
    """Create (or refresh/migrate) the worktree-local ``node_modules`` farm.

    A whole-directory symlink left by the previous implementation is replaced;
    an idempotent re-run only reconciles entries. A real ``node_modules`` the
    helper did not create is preserved (never destroyed).
    """
    if wt_node_modules.is_symlink():
        LOG.info(
            "Replacing whole-directory node_modules symlink %s with a link farm.",
            wt_node_modules,
        )
        _remove_farm_entry(wt_node_modules)
    elif os.path.lexists(wt_node_modules):
        if not wt_node_modules.is_dir():
            LOG.warning(
                "Worktree node_modules %s is not a directory; skipping link farm.",
                wt_node_modules,
            )
            return False
        if not _is_link_farm(wt_node_modules):
            LOG.info(
                "Worktree has a real node_modules (%s) not created by "
                "implement.py; leaving it untouched.",
                wt_node_modules,
            )
            return False

    if not os.path.lexists(wt_node_modules):
        try:
            wt_node_modules.mkdir(parents=True)
        except OSError as exc:
            LOG.warning(
                "Failed to create link farm directory %s: %s", wt_node_modules, exc
            )
            return False

    return _reconcile_link_farm(wt_node_modules, main_node_modules, overrides)


def _ensure_directory_symlink(wt_node_modules: Path, main_node_modules: Path) -> bool:
    """Degradation path: the previous whole-directory ``node_modules`` symlink.

    Used for repositories without a workspace package consumed by path, and
    kept idempotent/skip-when-present exactly as before.
    """
    if os.path.lexists(wt_node_modules):
        LOG.info(
            "Worktree already has node_modules (%s); skipping symlink.",
            wt_node_modules,
        )
        return False
    try:
        os.symlink(
            str(main_node_modules), str(wt_node_modules), target_is_directory=True
        )
    except (OSError, NotImplementedError) as exc:
        LOG.warning(
            "Failed to symlink node_modules into worktree %s: %s",
            wt_node_modules.parent,
            exc,
        )
        return False
    LOG.info("Symlinked %s -> %s", wt_node_modules, main_node_modules)
    return True


def _ensure_node_modules_symlink(worktree_path: str, repo_root: str | None) -> bool:
    """Materialise the worktree's ``node_modules`` as a worktree-local link farm.

    Git worktrees do not include gitignored directories, so tools spawned from
    the worktree (``<worktree>/node_modules/.bin/tsx``, vitest, tsx, …) fail
    with MODULE_NOT_FOUND. A whole-directory symlink to the main checkout's
    ``node_modules`` fixes that but resolves workspace packages consumed by
    path (``@worklog/shared`` → ``../../packages/shared``) back to the **main
    checkout's** package — so a worktree's ``packages/*/src`` change is
    invisible to its own build/test gate (false red / false green).

    The farm is a real ``<worktree>/node_modules`` directory holding:

    - one symlink per main-checkout entry (``.bin`` linked as a whole
      directory),
    - a real directory per scope (``@types``, ``@worklog``, …) holding one
      symlink per scoped package,
    - for every workspace package the main checkout consumes by path, a link
      to the **worktree's own** package directory instead (discovered via the
      ``workspaces`` field of the worktree's ``package.json``).

    It never descends into a dependency tree, never writes through the farm,
    and never installs anything. Re-running reconciles the farm with the main
    checkout's current dependency set (refresh path), migration from the old
    whole-directory symlink is automatic, and a real ``node_modules`` the
    helper did not create is preserved. Repositories without a workspace
    package consumed by path keep the previous whole-directory symlink.

    Failures are logged, never fatal: the worktree stays usable.

    Args:
        worktree_path: Absolute path to the worktree directory.
        repo_root: Absolute path to the main checkout, or None if unknown.

    Returns:
        True if the farm (or degradation symlink) was created/refreshed, False
        otherwise (skipped/failed).
    """
    if not repo_root:
        LOG.info("No repo root known; skipping node_modules link step.")
        return False
    main_node_modules = Path(repo_root) / "node_modules"
    if not main_node_modules.is_dir():
        LOG.info(
            "Main checkout has no node_modules (%s); skipping link step.",
            main_node_modules,
        )
        return False

    wt_node_modules = Path(worktree_path) / "node_modules"
    overrides = _workspace_link_overrides(Path(worktree_path), main_node_modules)
    if not overrides:
        return _ensure_directory_symlink(wt_node_modules, main_node_modules)

    return _materialise_link_farm(wt_node_modules, main_node_modules, overrides)


def _shared_resolution_check(
    worktree_path: str, repo_root: str | None
) -> dict[str, Any]:
    """Report whether the worktree resolves the shared workspace package locally.

    The reference case is ``@worklog/shared`` consumed by path: the main
    checkout's ``node_modules/@worklog/shared`` is a relative symlink to its
    own ``packages/shared``, so only a worktree-local override makes the
    worktree's package (and its ``dist``) visible to its gate.

    Single source of truth for both phases: ``start`` reports the result in
    its JSON report (best-effort, warning only) and ``finish`` hard-fails on
    it when the worktree's change set touches the shared source
    (SA-0MVPI41SR008JYXL). The launch-and-read-back contract is therefore
    identical in both places.

    Returns a dict with ``applicable``, ``ok``, ``package``,
    ``expected_root``, ``resolved_root`` and ``stale_dist`` keys. ``applicable``
    is False (``ok`` None) when the main checkout does not declare the shared
    workspace package — the degradation path, where the check is a no-op. A
    missing ``node_modules`` is *stale*, not "not applicable".

    Args:
        worktree_path: Absolute path to the worktree directory.
        repo_root: Absolute path to the main checkout, or None if unknown.
    """
    main = Path(repo_root) if repo_root else None
    main_package = main / SHARED_WORKSPACE_PACKAGE_DIR if main else None
    main_package_json = _read_package_json(main_package) if main_package else {}
    if (
        not main_package
        or main_package_json.get("name") != SHARED_WORKSPACE_PACKAGE_NAME
    ):
        return {
            "applicable": False,
            "ok": None,
            "package": SHARED_WORKSPACE_PACKAGE_NAME,
            "expected_root": None,
            "resolved_root": None,
            "stale_dist": None,
        }

    wt = Path(worktree_path)
    expected_root = (wt / SHARED_WORKSPACE_PACKAGE_DIR).resolve()
    resolved_link = wt / "node_modules" / Path(
        *SHARED_WORKSPACE_PACKAGE_NAME.split("/")
    )
    resolved_root = (
        Path(os.path.realpath(resolved_link))
        if os.path.lexists(resolved_link)
        else None
    )
    ok = resolved_root is not None and resolved_root == expected_root
    return {
        "applicable": True,
        "ok": ok,
        "package": SHARED_WORKSPACE_PACKAGE_NAME,
        "expected_root": str(expected_root),
        "resolved_root": str(resolved_root) if resolved_root else None,
        "stale_dist": (
            None
            if ok
            else str((main_package / "dist").resolve())
        ),
    }


def _prepare_node_modules(
    worktree_path: str, repo_root: str | None
) -> dict[str, Any]:
    """Run the node_modules link step and the shared-resolution check.

    Returns the ``start`` report block: ``link_created`` (whether this run
    materialised/refreshed the farm) and ``shared_resolution`` (the
    :func:`_shared_resolution_check` result), with ``shared_resolution_ok``
    mirrored for convenient JSON consumption (SA-0MVPI400Y001FOM1 AC2).
    """
    link_created = _ensure_node_modules_symlink(worktree_path, repo_root)
    resolution = _shared_resolution_check(worktree_path, repo_root)
    return {
        "link_created": link_created,
        "shared_resolution": resolution,
        "shared_resolution_ok": resolution["ok"],
    }


def _worktree_change_set_paths(
    worktree_path: str, parent_branch: str = DEFAULT_PARENT_BRANCH
) -> list[str] | None:
    """List the worktree's repository-relative change-set paths (read-only).

    The change set is what ``finish`` would commit: uncommitted files
    (``git status --porcelain=v1 --untracked-files=all``) plus committed
    changes since the parent branch's merge-base
    (``git diff --name-only <parent_branch>...HEAD``). A rename is reported by
    its destination path.

    Args:
        worktree_path: Absolute path to the worktree directory.
        parent_branch: Branch the worktree forked from (``dev``).

    Returns:
        A sorted, de-duplicated list of repository-relative paths, or ``None``
        when the change set cannot be read (undecidable). Callers must not
        interpret ``None`` as "no changes": the stale-resolution gate no-ops
        on it rather than blocking every finish in a worktree whose git state
        it cannot read.
    """
    status = run_cmd(
        [
            "git", "-C", str(worktree_path), "status", "--porcelain=v1",
            "--untracked-files=all",
        ],
        check=False,
        timeout=60,
    )
    if status.returncode != 0:
        LOG.warning(
            "Cannot read the change set of %s (git status exit %s): %s",
            worktree_path,
            status.returncode,
            (status.stderr or "").strip()[:500],
        )
        return None

    paths: set[str] = set()
    for line in (status.stdout or "").splitlines():
        if len(line) < 4:
            continue
        entry = line[3:].strip()
        if not entry:
            continue
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        paths.add(entry.strip('"'))

    committed = run_cmd(
        [
            "git", "-C", str(worktree_path), "diff", "--name-only",
            "--no-renames", f"{parent_branch}...HEAD",
        ],
        check=False,
        timeout=60,
    )
    if committed.returncode == 0:
        paths.update(
            path.strip()
            for path in (committed.stdout or "").splitlines()
            if path.strip()
        )
    else:
        LOG.debug(
            "Cannot diff %s against %s (exit %s); relying on the dirty set only",
            worktree_path,
            parent_branch,
            committed.returncode,
        )
    return sorted(paths)


def _touches_shared_source(paths: list[str]) -> bool:
    """Whether any repository-relative path is under ``packages/shared/src/``."""
    source_prefix = SHARED_WORKSPACE_PACKAGE_DIR / "src"
    prefix = source_prefix.as_posix().rstrip("/") + "/"
    return any(
        path.replace("\\", "/").startswith(prefix) for path in paths
    )


def _stale_shared_resolution(
    worktree_path: str,
    repo_root: str | None,
    parent_branch: str = DEFAULT_PARENT_BRANCH,
) -> str | None:
    """Refusal message when shared source changed while resolution is stale.

    The ``finish`` gate's detector (SA-0MVPI41SR008JYXL, parent
    SA-0MVMZBJGT00718ZO). It fires **only** when both hold:

    1. the worktree's change set touches ``packages/shared/src/**`` — a
       committed change (ahead of the parent branch's merge-base) or an
       uncommitted one — and
    2. :func:`_shared_resolution_check` is applicable and not ``ok``, i.e.
       ``@worklog/shared`` still resolves outside this worktree (typically the
       main checkout's stale ``packages/shared/dist``).

    Either condition alone is a no-op: an unchanged shared package never
    blocks a worktree, and a stale resolution is only a hazard for a change
    that the gate could not see. Repositories without a workspace shared
    package take the degradation path and are never refused.

    Args:
        worktree_path: Absolute path to the worktree directory.
        repo_root: Absolute path to the main checkout, or None if unknown.
        parent_branch: Branch the worktree forked from (``dev``).

    Returns:
        An actionable multi-line message naming the stale
        ``<main-checkout>/packages/shared/dist`` path and the remediation, or
        ``None`` when the gate must not fire.
    """
    check = _shared_resolution_check(worktree_path, repo_root)
    if not check["applicable"] or check["ok"]:
        return None

    changed = _worktree_change_set_paths(worktree_path, parent_branch)
    if changed is None or not _touches_shared_source(changed):
        return None

    source_rel = (SHARED_WORKSPACE_PACKAGE_DIR / "src").as_posix()
    expected_root = check["expected_root"] or (
        "<worktree>/" + SHARED_WORKSPACE_PACKAGE_DIR.as_posix()
    )
    resolved_root = check["resolved_root"] or "<unresolved: no node_modules entry>"
    stale_dist = check["stale_dist"] or str(
        (Path(repo_root) / SHARED_WORKSPACE_PACKAGE_DIR / "dist")
        if repo_root
        else SHARED_WORKSPACE_PACKAGE_DIR / "dist"
    )
    return (
        f"Stale {SHARED_WORKSPACE_PACKAGE_NAME} resolution: this worktree "
        f"changes {source_rel}/** but the package resolves outside it, so the "
        f"test gate would validate the main checkout's stale build instead of "
        f"the worktree's source.\n"
        f"  resolved:    {resolved_root}\n"
        f"  expected:    {expected_root}\n"
        f"  stale dist:  {stale_dist}\n"
        f"Refusing to run the gate on the wrong code — no commit was made.\n"
        f"Remediation: re-run `python3 skill/implement/scripts/implement.py "
        f"start <work-item-id>` from the main checkout to refresh this "
        f"worktree's node_modules link farm (the link step is idempotent), "
        f"then rebuild and run `implement.py finish` again."
    )


# Directories pruned by the nested node_modules discovery walk. ``.git`` and
# ``.worklog`` are large/irrelevant trees; ``node_modules`` is pruned so the
# walk never descends into (and therefore never mirrors) a dependency tree's
# own nested dependencies (OSL-0MUFG3PJA00379CT).
_NESTED_NODE_MODULES_PRUNE_DIRS = frozenset({".git", ".worklog", "node_modules"})


def _iter_nested_node_modules(repo_root: str) -> Iterator[tuple[str, str]]:
    """Yield nested ``node_modules`` directories under the main checkout.

    Walks *repo_root* top-down, pruning :data:`_NESTED_NODE_MODULES_PRUNE_DIRS`
    (so ``.git``/``.worklog`` trees are ignored and a ``node_modules``
    directory is never descended into) and never following symlinked
    directories. The repository-root ``node_modules`` is excluded — it is
    owned by :func:`_ensure_node_modules_symlink`.

    Args:
        repo_root: Absolute path to the main checkout.

    Yields:
        ``(rel_parent, rel_node_modules)`` POSIX-style paths relative to
        *repo_root*, e.g. ``(".pi/packages/x", ".pi/packages/x/node_modules")``.
    """
    repo = Path(repo_root)
    for dirpath, dirnames, _filenames in os.walk(repo, topdown=True):
        current = Path(dirpath)
        rel_parent = os.path.relpath(current, repo)
        # A nested candidate is a child named node_modules, but not the
        # repository-root one (handled separately).
        is_nested_candidate = rel_parent != "." and "node_modules" in dirnames
        # Prune heavy/irrelevant trees, dependency trees, and symlinked
        # directories (never follow — avoids loops and branch shadowing).
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _NESTED_NODE_MODULES_PRUNE_DIRS
            and not (current / name).is_symlink()
        )
        if not is_nested_candidate:
            continue
        if not (current / "node_modules").is_dir():  # pragma: no cover - defensive
            continue
        yield rel_parent, os.path.join(rel_parent, "node_modules")


def _ensure_nested_node_modules_symlinks(
    worktree_path: str, repo_root: str | None
) -> int:
    """Auto-symlink the main checkout's *nested* node_modules into a worktree.

    Companion to :func:`_ensure_node_modules_symlink`, which materialises the
    repository-root ``node_modules`` (link farm, or the degradation
    whole-directory symlink). On checkouts whose dependencies live in a
    nested/workspace package (for example a pi package under
    ``.pi/packages/<pkg>/node_modules``) the worktree would otherwise have no
    dependencies at all, so tools run from the worktree (``pi``, ``tsx``) fail
    with ``Cannot find module``.

    For every nested ``node_modules`` directory discovered in the main checkout
    (:func:`_iter_nested_node_modules`) this creates
    ``<worktree>/<rel>/node_modules`` as a symlink to
    ``<main-checkout>/<rel>/node_modules`` when BOTH conditions hold:

    - the worktree has no entry at that path (dir or symlink) — never
      overwrite; AND
    - the parent directory ``<worktree>/<rel>`` already exists — never
      fabricate a package directory the branch removed (avoids shadowing
      branch state).

    Best-effort only: a per-candidate failure is logged at ``WARNING`` and does
    not abort; remaining candidates are still processed. Symlinks only — the
    helper never copies, installs, or invokes a package manager.

    Args:
        worktree_path: Absolute path to the worktree directory.
        repo_root: Absolute path to the main checkout, or None if unknown.

    Returns:
        Number of nested node_modules symlinks created.
    """
    if not repo_root:
        LOG.info("No repo root known; skipping nested node_modules symlinks.")
        return 0

    wt_root = Path(worktree_path)
    repo = Path(repo_root)
    created = 0

    for rel_parent, rel_nm in _iter_nested_node_modules(repo_root):
        wt_nm = wt_root / rel_nm
        if os.path.lexists(wt_nm):
            LOG.info(
                "Worktree already has %s; skipping nested node_modules symlink.",
                wt_nm,
            )
            continue
        wt_parent = wt_root / rel_parent
        if not wt_parent.is_dir():
            LOG.info(
                "Worktree has no %s; skipping nested node_modules symlink "
                "(would fabricate a package directory).",
                wt_parent,
            )
            continue
        main_nm = repo / rel_nm
        try:
            os.symlink(str(main_nm), str(wt_nm), target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            LOG.warning(
                "Failed to symlink nested node_modules %s -> %s: %s",
                wt_nm,
                main_nm,
                exc,
            )
            continue
        LOG.info("Symlinked %s -> %s", wt_nm, main_nm)
        created += 1

    if created:
        LOG.info("Created %d nested node_modules symlink(s).", created)
    return created


def _ensure_submodules(worktree_path: str, repo_root: str | None = None) -> bool:
    """Initialize git submodules inside a newly-created worktree.

    ``git worktree add`` does not automatically initialize submodules.  This
    helper runs ``git submodule update --init --recursive`` inside the
    worktree so that downstream builds and tests see the expected submodule
    content.

    Best-effort only — submodule initialisation failure is **never fatal**.
    On failure a warning is logged and the worktree is left usable.

    When the main checkout has no ``.gitmodules`` the command is a no-op and
    returns ``False``.

    Args:
        worktree_path: Absolute path to the worktree directory.
        repo_root: Absolute path to the main checkout (unused but kept for
            API symmetry with ``_ensure_node_modules_symlink``).

    Returns:
        ``True`` if submodules were initialised (or there were none),
        ``False`` if the command failed or there was nothing to do.
    """
    # Quick check: does the repo even have submodules?
    gitmodules = Path(repo_root) / ".gitmodules" if repo_root else None
    if gitmodules and not gitmodules.is_file():
        LOG.info("No .gitmodules found at %s; skipping submodule init.", gitmodules)
        return False

    try:
        result = subprocess.run(
            ["git", "submodule", "update", "--init", "--recursive"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=600,  # 10 min — generous for large submodule trees.
            check=False,
        )
        if result.returncode != 0:
            LOG.warning(
                "Submodule initialisation failed in worktree %s (rc=%d): %s",
                worktree_path,
                result.returncode,
                result.stderr.strip() or result.stdout.strip(),
            )
            return False
        LOG.info(
            "Submodules initialised in worktree %s.", worktree_path
        )
        return True
    except (subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError) as exc:
        LOG.warning(
            "Submodule initialisation errored in worktree %s: %s",
            worktree_path,
            exc,
        )
        return False


def _should_defer_worktree_removal(
    keep_worktree: bool = False,
) -> tuple[bool, str]:
    """Whether this process must leave the worktree in place.

    One predicate, one owner per worktree (SA-0MVTE8XAJ002IUDR):

    - **driven child session** (``IMPLEMENT_DRIVE_ACTIVE=1``) — the worktree
      is the session's cwd; ``implement.py drive`` removes it once that
      session has actually exited. Reason token: ``driven-session``.
    - **explicit opt-in** — ``IMPLEMENT_KEEP_WORKTREE=1`` or the parsed
      ``--keep-worktree`` flag defer removal for any session; the caller (the
      session itself, or an operator cleanup pass) owns the removal.
      Reason tokens: ``keep-worktree-env`` / ``keep-worktree-flag``.
    - **everything else** — ``finish``/``abort`` remove the tree exactly as
      before (``deferred`` is ``False`` and the reason is empty).

    Precedence is fixed and stable (driven > env > flag) so the reason token
    is deterministic when switches are combined; combining them is
    idempotent — the tree is deferred once, never twice.

    Args:
        keep_worktree: The parsed ``--keep-worktree`` flag.

    Returns:
        ``(deferred, reason)`` — ``reason`` is a stable token, empty when the
        removal should proceed.
    """
    if os.environ.get(DRIVE_RECURSION_ENV) == "1":
        return True, "driven-session"
    if os.environ.get(KEEP_WORKTREE_ENV) == "1":
        return True, "keep-worktree-env"
    if keep_worktree:
        return True, "keep-worktree-flag"
    return False, ""


def _worktree_cleanup_command(work_item_id: str) -> str:
    """The exact follow-up command that removes a retained worktree.

    One source of truth for the deferred-removal surface (completion comment,
    JSON report and human output) of a **plain leaf** session that opted in
    with ``finish --keep-worktree`` / ``IMPLEMENT_KEEP_WORKTREE=1``
    (SA-0MVTHJ3PJ000IVSK AC2). The command is the reviewed ``cleanup`` action
    — never a raw ``git worktree remove`` — so the retained tree is removed
    behind the shared safety gates (terminal item, clean tree, worktree not
    main checkout), and re-running it is a reported no-op.

    Args:
        work_item_id: The work item that owns the retained worktree.

    Returns:
        A copy-pasteable command string.
    """
    return (
        f"python3 skill/implement/scripts/implement.py cleanup {work_item_id}"
    )


def _registered_worktree_paths(repo_root: str | None) -> set[str] | None:
    """Return the resolved paths git registers as worktrees of *repo_root*.

    This is the ownership record a removal surface must be confined to: a
    directory git does not list as a worktree of this repository is not ours
    to delete, however it was named (SA-0MVXVC0KB004XDH0). The list includes
    the main working tree itself, which the callers refuse separately.

    Args:
        repo_root: The main checkout root to ask git about, or ``None`` when
            no root could be resolved.

    Returns:
        The set of resolved absolute worktree paths, or ``None`` when the
        registry cannot be read (no root, not a git working tree, or git
        unavailable) — callers treat ``None`` as "cannot prove ownership"
        and fail closed.
    """
    if not repo_root:
        return None
    result = run_cmd(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo_root,
        check=False,
    )
    if result.returncode != 0:
        return None
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = line[len("worktree "):].strip()
        if not candidate:
            continue
        try:
            paths.add(str(Path(candidate).resolve()))
        except OSError:
            continue
    return paths


def _remove_worktree(worktree_path: str, repo_root: str | None = None) -> bool:
    """Remove a worktree directory gracefully.

    Safety gates:
    1. Validates the target path is not the repository root. The root is
       taken from the *repo_root* argument when provided (``phase_finish``
       passes the authoritative value from the implement state file);
       otherwise it is derived from the current directory, resolving to the
       MAIN checkout root even when the current directory is inside a linked
       worktree (see ``_get_repo_root``).
    2. Refuses any target git does not register as a worktree of that root
       (:func:`_registered_worktree_paths`) — the surface is confined to the
       named worktree, so a mistyped/stale path can never delete an unrelated
       directory that merely happens to be outside the repository and free of
       a ``.git/`` directory (SA-0MVXVC0KB004XDH0). A target that no longer
       exists is a no-op success (nothing to remove), preserving idempotency.
    3. Before manual (shutil) removal, checks that the target does
       NOT contain a ``.git/`` directory (which would mean it's the
       main working tree).

    Args:
        worktree_path: Path to the worktree.
        repo_root: Optional explicit main repo root (defaults to discovery
            from the current directory).

    Returns:
        True if removal succeeded.

    Raises:
        RuntimeError: If the target path matches the repo root, is not a
            worktree registered with that root, or contains a ``.git``
            directory (main working tree).
    """
    resolved_path = str(Path(worktree_path).resolve())

    # ── Safety gate 1: refuse if target is the repo root ──────────
    repo_root = repo_root or _get_repo_root()
    if repo_root and Path(resolved_path) == Path(repo_root).resolve():
        msg = (
            f"REFUSE to remove the repository root: {resolved_path}. "
            f"This is the main working tree, not a worktree. "
            f"Check for stale .implement_state.json in the repo root."
        )
        LOG.critical(msg)
        raise RuntimeError(msg)

    # ── Safety gate 2: refuse anything this repository does not own ──
    # ``git worktree remove`` refuses a non-worktree on its own, but the
    # manual ``shutil.rmtree`` fallback below ran unconditionally — so a
    # path outside the repository with no ``.git/`` directory was deleted
    # outright. Confine the surface to the worktrees git registers here.
    if Path(resolved_path).exists():
        registered = _registered_worktree_paths(repo_root)
        if registered is None or resolved_path not in registered:
            owner = repo_root or "the resolved repository root"
            msg = (
                f"REFUSE to remove {resolved_path}: it is not a worktree "
                f"registered with {owner}. Removal is confined to the named "
                f"worktree; an unregistered directory is never deleted. If "
                f"the worktree registration is stale, run `git worktree "
                f"prune` first."
            )
            LOG.critical(msg)
            raise RuntimeError(msg)
    else:
        # Nothing to delete, but the registration may still be stale: prune
        # (from the resolved root only) so the registry matches reality.
        if repo_root:
            run_cmd(["git", "worktree", "prune"], cwd=repo_root, check=False)
        LOG.info(
            "Worktree already absent, nothing to remove: %s", resolved_path
        )
        return True

    # If the current directory is inside the worktree being removed, step
    # out to the repo root first. A deleted cwd breaks os.getcwd() and
    # subprocess spawning for every later step (StatusLifecycle.__exit__,
    # phase_finish's completion comment, the signal handler).
    try:
        current_cwd = Path.cwd().resolve()
    except OSError:
        current_cwd = None
    target = Path(resolved_path)
    if (
        current_cwd is not None
        and (current_cwd == target or target in current_cwd.parents)
    ):
        os.chdir(repo_root or str(Path.home()))

    LOG.info("Removing worktree: %s", resolved_path)
    result = run_cmd(
        ["git", "worktree", "remove", "--force", resolved_path],
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        LOG.warning(
            "git worktree remove failed, trying manual removal: %s",
            result.stderr.strip(),
        )

        # ── Safety gate 3: refuse if target has a .git/ directory ──
        target = Path(resolved_path)
        git_dir = target / ".git"
        if git_dir.is_dir():
            msg = (
                f"REFUSE to manually remove {resolved_path}: it contains "
                f"a .git/ directory, indicating the main working tree. "
                f"This would destroy the entire repository. Aborting."
            )
            LOG.critical(msg)
            raise RuntimeError(msg)

        # Fall back to manual removal (safe now - .git/ check passed)
        try:
            import shutil
            shutil.rmtree(resolved_path, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Manual worktree removal failed: %s", exc)
            return False

    # Prune stale worktree references. Run from an explicit, still-valid
    # directory: the current directory may have been the worktree that was
    # just removed (finish/abort can run from inside it), and a deleted cwd
    # breaks both the log line and the subprocess spawn.
    run_cmd(["git", "worktree", "prune"], cwd=repo_root, check=False)
    return True


def _restore_repo_state(repo_root: str) -> None:
    """Restore the repo to the dev branch.

    Deliberately **outside** the per-repository lock: this runs on the abort
    path (signal handler / crash cleanup), where the process may already hold
    the lock — a second ``flock`` on a new descriptor would fail against its
    own hold and skip the restore. A dying process releases the lock itself
    (kernel-guaranteed), so no other run can be interleaving at this point.

    Args:
        repo_root: Path to the repo root.
    """
    run_cmd(["git", "checkout", DEFAULT_PARENT_BRANCH], cwd=repo_root, check=False)
    run_cmd(["git", "pull", "origin", DEFAULT_PARENT_BRANCH], cwd=repo_root, check=False)


def _sync_parent_branch(
    repo_root: str, parent_branch: str = DEFAULT_PARENT_BRANCH
) -> dict[str, Any]:
    """Sync the main checkout's local parent branch to origin/<parent_branch>.

    After ``implement.py finish`` pushes a child branch to ``origin/<parent>",
    the local parent branch can be left behind.  This helper fast-forwards
    the local parent branch so that the next ``phase_start`` / child
    worktree starts from an up-to-date base (AC1 / AC3).

    Safety (AC2):
        When the update is not safe — dirty working tree, checkout is on
        another branch or detached, or the local branch has diverged — the
        helper returns a *skipped* result without failing.  Local commits
        are never lost.

    Args:
        repo_root: Path to the repo root.
        parent_branch: The parent branch to sync (default: ``dev``).

    Returns:
        Dict with ``method`` ("synced" | "skipped"), ``success`` (bool),
        and optional ``reason`` / ``warning``.
    """
    result: dict[str, Any] = {
        "method": "none",
        "success": True,
    }

    # ── Pre-condition checks (safe-skip semantics) ─────────────────

    # 1. Dirty working tree → skip (check the main checkout, not the
    #    worktree we may currently be invoked from)
    status_output = git_status(repo_root)
    if git_has_dirty_files(status_output):
        result["method"] = "skipped"
        result["success"] = True
        result["reason"] = "dirty_working_tree"
        result["warning"] = (
            f"Skipped syncing parent branch {parent_branch}: "
            "dirty working tree detected."
        )
        LOG.info("%s", result["warning"])
        return result

    # 2. Fetch the latest parent ref. Remote unreachable → degrade
    #    gracefully (report, never fail a finish that already pushed).
    fetch_result = run_cmd(
        ["git", "fetch", "origin", parent_branch],
        cwd=repo_root,
        check=False,
        timeout=120,
    )
    if fetch_result.returncode != 0:
        result["method"] = "skipped"
        result["success"] = True
        result["reason"] = "fetch_failed"
        result["warning"] = (
            f"Skipped syncing parent branch {parent_branch}: "
            f"git fetch origin {parent_branch} failed. "
            f"The finish push may have succeeded but local {parent_branch} "
            f"was not updated."
        )
        LOG.info("%s", result["warning"])
        return result

    # 3. Determine the current branch of the main checkout
    branch_result = run_cmd(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root,
        check=False,
    )
    if branch_result.returncode != 0:
        result["method"] = "skipped"
        result["success"] = True
        result["reason"] = "cannot_determine_branch"
        result["warning"] = "Could not determine current branch."
        LOG.info("%s", result["warning"])
        return result

    current_branch = branch_result.stdout.strip()
    # Use the fully-qualified remote-tracking ref so a stale local branch
    # literally named ``origin/<parent>`` cannot shadow it (git would
    # otherwise warn and resolve to the local branch).
    remote_parent_ref = f"refs/remotes/origin/{parent_branch}"

    if current_branch == parent_branch:
        # 4a. Checked out on the parent branch: fast-forward with
        #     ``merge --ff-only`` (refuses on divergence; never rewrites
        #     the working tree or loses local commits).
        ff_result = run_cmd(
            ["git", "merge-base", "--is-ancestor",
             "HEAD", remote_parent_ref],
            cwd=repo_root,
            check=False,
        )
        if ff_result.returncode != 0:
            result["method"] = "skipped"
            result["success"] = True
            result["reason"] = "diverged"
            result["warning"] = (
                f"Skipped syncing parent branch {parent_branch}: "
                f"local {parent_branch} has diverged from "
                f"origin/{parent_branch}; --ff-only is not possible "
                f"without risking local commits."
            )
            LOG.info("%s", result["warning"])
            return result

        merge_result = run_cmd(
            ["git", "merge", "--ff-only", remote_parent_ref],
            cwd=repo_root,
            check=False,
        )
        if merge_result.returncode != 0:
            result["method"] = "skipped"
            result["success"] = True
            result["reason"] = "merge_failed"
            result["warning"] = (
                f"Skipped syncing parent branch {parent_branch}: "
                f"--ff-only merge failed (unexpected)."
            )
            LOG.info("%s", result["warning"])
            return result
    else:
        # 4b. Not checked out on the parent branch (another branch or
        #     detached HEAD): update the local ref directly via
        #     ``git fetch origin <parent>:<parent>``. Git refuses a
        #     non-fast-forward update, so local commits are never lost,
        #     and no branch switch is performed.
        ref_fetch_result = run_cmd(
            ["git", "fetch", "origin",
             f"{parent_branch}:{parent_branch}"],
            cwd=repo_root,
            check=False,
            timeout=120,
        )
        if ref_fetch_result.returncode != 0:
            result["method"] = "skipped"
            result["success"] = True
            result["reason"] = "diverged_or_checked_out"
            result["warning"] = (
                f"Skipped syncing parent branch {parent_branch}: "
                f"current branch is '{current_branch}'; the local "
                f"{parent_branch} ref was not updated (diverged or "
                f"cannot be fetched non-checked-out)."
            )
            LOG.info("%s", result["warning"])
            return result

    result["method"] = "synced"
    result["target"] = remote_parent_ref
    LOG.info("Synced parent branch %s to origin/%s", parent_branch, parent_branch)
    return result


def _sync_parent_branch_locked(
    repo_root: str,
    parent_branch: str = DEFAULT_PARENT_BRANCH,
    work_item_id: str = "",
    timeout: float | None = None,
) -> dict[str, Any]:
    """Sync the local parent branch while holding the per-repository lock.

    ``phase_start`` refreshes the parent branch before forking a worktree, and
    that refresh fast-forwards the **shared** local parent ref — the very ref a
    concurrent ``finish`` moves inside its locked commit → fast-forward → push
    → sync window. Taking the same per-repository lock keeps the two windows
    from interleaving (SA-0MVPZG3IQ009IJMO): a refresh that ran between a
    finish's local fast-forward and its push could move the shared ref and make
    the finish's own fast-forward fail, or leave the shared ref pointing at an
    unpublished commit.

    **Fail-open by design.** ``start`` must never be blocked or failed by a
    concurrent finish, so a held lock is waited on only up to *timeout*
    (default :data:`DEFAULT_START_LOCK_TIMEOUT`, env
    ``IMPLEMENT_START_LOCK_TIMEOUT``) and then the refresh is skipped with a
    logged warning — the local branch is still a perfectly usable base.

    Args:
        repo_root: Path to the main checkout (where the shared ref lives).
        parent_branch: Parent branch to sync (default ``dev``).
        work_item_id: Work item this run belongs to (named in refusal text).
        timeout: Bounded wait for the lock; ``None`` resolves the default.

    Returns:
        The :func:`_sync_parent_branch` result, plus a ``lock`` sub-dict
        describing the acquisition. On a held lock: ``method="skipped"``,
        ``success=True``, ``reason="lock_held"``.
    """
    if timeout is None:
        timeout = _start_lock_timeout()
    lock_fd, refusal = acquire_main_checkout_lock(
        repo_root, work_item_id, timeout=timeout
    )
    lock_report = {
        "lock_path": str(_main_checkout_lock_path(repo_root)),
        "acquired": lock_fd is not None,
        "timeout_seconds": timeout,
        "holder": refusal if lock_fd is None else None,
    }
    if lock_fd is None:
        result: dict[str, Any] = {
            "method": "skipped",
            "success": True,
            "reason": "lock_held",
            "lock": lock_report,
            "warning": (
                f"Skipped refreshing parent branch {parent_branch}: a "
                f"concurrent run holds the repository lock "
                f"({lock_report['lock_path']}). {refusal}"
            ),
        }
        LOG.info("%s", result["warning"])
        return result
    try:
        result = _sync_parent_branch(repo_root, parent_branch)
    finally:
        _release_main_checkout_lock(lock_fd)
    result["lock"] = lock_report
    return result


# ---------------------------------------------------------------------------
# Verified, reversible local-dev sync + post-push ancestry assertion
# (SA-0MVPZG7A0000N66U, parent SA-0MVNQURZM001TKJY AC1/AC2)
# ---------------------------------------------------------------------------


def _resolve_full_commit(cwd: str, rev: str) -> str | None:
    """Full 40-character commit hash for *rev* in *cwd*, or ``None``.

    Ref-membership decisions are never taken on an abbreviated hash:
    ``git_get_commit_hash`` returns a short hash (fine to display, unsafe to
    assert with), so every ancestry check resolves the commit to its full
    object name first.
    """
    if not rev:
        return None
    result = run_cmd(
        ["git", "rev-parse", "--verify", f"{rev}^{{commit}}"],
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _resolve_origin_parent_tip(
    repo_root: str, parent_branch: str = DEFAULT_PARENT_BRANCH
) -> str | None:
    """Full hash of ``refs/remotes/origin/<parent_branch>``, or ``None``."""
    return _resolve_full_commit(
        repo_root, f"refs/remotes/origin/{parent_branch}"
    )


def _push_cwd_has_tracked_changes(cwd: str) -> bool:
    """Whether *cwd* has tracked files modified or staged (untracked ignored).

    A tracked change in the push checkout means the worktree's tree is not the
    pushed commit's tree, and a recovery rebase cannot carry it safely, so the
    subject guard refuses rather than let the gate verify (and cache) a tree
    the pushed commit does not describe. Untracked files are ignored because a
    rebase can usually proceed with them present. Fail-closed: an unreadable
    status is treated as dirty.
    """
    try:
        result = run_cmd(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=cwd,
            check=False,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        LOG.debug("Push-subject status check failed for %s: %s", cwd, exc)
        return True
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


def _assert_push_subject(
    push_cwd: str,
    commit_hash: str,
    branch: str,
    worktree_path: str,
) -> None:
    """Assert *push_cwd* holds *commit_hash* before the dev push.

    ``.githooks/pre-push`` resolves the pushed commit as the gate's subject
    (``run_tests.py --gate-commit``) and runs the full suite against a clean
    checkout holding it. ``finish`` therefore pushes from the worktree that
    has the commit checked out, so the gate always measures the pushed commit;
    this guard proves that invariant **before** ``git push`` runs and fails
    closed when the push directory cannot be the subject: it does not exist,
    is not a git working tree, does not contain the commit, its ``HEAD`` is a
    different commit, or it has tracked uncommitted changes that a recovery
    rebase cannot carry. Pushing anyway would let the gate verify (and cache)
    a tree the pushed commit does not describe
    (SA-0MVTFMB7K002Y1F3 AC1/AC2).

    Args:
        push_cwd: Directory the push will run from (the worktree).
        commit_hash: The commit being pushed (short or full hash).
        branch: Feature branch being pushed (for the refusal message).
        worktree_path: The worktree the push requires (for the refusal
            message; normally equal to *push_cwd*).

    Raises:
        PushSubjectMismatchError: when *push_cwd* does not hold *commit_hash*.
    """
    reason = ""
    actual_head: str | None = None
    if not push_cwd or not Path(push_cwd).is_dir():
        reason = "the push directory does not exist"
    else:
        actual_head = _resolve_full_commit(push_cwd, "HEAD")
        if actual_head is None:
            reason = "the push directory is not a git working tree"
        else:
            pushed = _resolve_full_commit(push_cwd, commit_hash)
            if pushed is None:
                reason = (
                    f"the push directory does not contain commit {commit_hash}"
                )
            elif pushed != actual_head:
                reason = (
                    f"the push directory's HEAD ({actual_head}) is not the "
                    f"pushed commit ({pushed})"
                )
            elif _push_cwd_has_tracked_changes(push_cwd):
                reason = (
                    "the push directory has tracked uncommitted changes that "
                    "a recovery rebase cannot carry"
                )
    if not reason:
        return
    raise PushSubjectMismatchError(
        commit_hash=commit_hash,
        branch=branch,
        worktree_path=worktree_path,
        actual_head=actual_head,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Finish-failure classification vocabulary (LP-0MVWEP1MM0097RAT)
# ---------------------------------------------------------------------------
#
# The single, table-driven classifier that turns a failing ``finish``
# subprocess report into ``{kind, step, reason, message}`` so
# ``phase_drive`` can tell a transient infrastructure refusal from a genuine
# gate failure (parent epic LP-0MVVYQ1U3006GDWT AC1). The constants live
# BESIDE the emitters (``_fast_forward_local_dev_for_push`` and
# ``phase_finish``) intentionally:
#
# **Drift rule** — every ``(step, reason)`` pair the shipped ``finish`` code
# can emit must be listed in the vocabulary below in the same commit that
# introduces it (a new ``_refuse`` reason or failure exit that is not listed
# silently classifies as ``unknown`` — fail-safe, never ``infrastructure``,
# but hidden from the operator). The table test in
# ``tests/test_implement_drive_recovery.py`` enumerates the emitted pairs
# against this vocabulary.

#: The three failure kinds :func:`classify_finish_failure` can return.
FINISH_FAILURE_KINDS: tuple[str, ...] = (
    "infrastructure",
    "gate",
    "unknown",
)

#: Wildcard reason matched against :data:`FINISH_GATE_REFUSALS`: an entry
#: ``("step", "*")`` classifies as ``gate`` for any reason under that step
#: (e.g. ``("worktree_placement", "*")`` — the placement violation is a
#: gate failure regardless of the specific reason the exit records).
FINISH_REASON_WILDCARD = "*"

#: Allow-list of ``(step, reason)`` pairs classified as **infrastructure** —
#: transient refusals that self-clear (a concurrent session's fast-forward,
#: a held main-checkout lock, a dropped push connection, a failed ancestry
#: assertion) and are retried by drive on a separate budget instead of
#: consuming the finish-attempt gate bound (parent epic LP-0MVVYQ1U3006GDWT
#: Assumptions).
FINISH_INFRA_REFUSALS: frozenset[tuple[str, str]] = frozenset({
    ("local_dev_fast_forward", "local_dev_diverged"),
    ("local_dev_fast_forward", "commit_not_based_on_remote_tip"),
    ("local_dev_fast_forward", "fetch_failed"),
    ("local_dev_fast_forward", "checkout_failed"),
    ("local_dev_fast_forward", "no_remote_tracking_ref"),
    ("main_checkout_lock", "lock_held"),
    ("main_checkout_guard", "fail"),
    ("push", "timeout"),
    ("push", "connection_closed"),
    ("push", "transport"),
    ("ancestry_assertion", "ancestry_failed"),
})

#: Explicit ``(step, reason)`` pairs classified as **gate** failures —
#: genuine failures that consume the finish-attempt budget and are never
#: retried as infrastructure. Recorded for documentation/consistency AND
#: used by the classifier; anything not listed in either set is
#: ``unknown`` (fail-safe). A ``FINISH_REASON_WILDCARD`` entry matches any
#: reason under the step.
FINISH_GATE_REFUSALS: frozenset[tuple[str, str]] = frozenset({
    ("tests", "tests_failed"),
    ("build", "build_failed"),
    ("build_churn", FINISH_REASON_WILDCARD),
    ("worktree_placement", FINISH_REASON_WILDCARD),
    ("diff", "no_changes"),
})

#: Consultation order for the structured ``steps`` payload of a failing
#: ``finish`` report. When several steps failed, the first in this order is
#: the primary cause; the mandated prefix (``main_checkout_lock`` → … →
#: ``ancestry_assertion``) is followed by the two vocabulary steps that
#: return before the lock step (``worktree_placement``, ``diff``).
FINISH_STEP_PRECEDENCE: tuple[str, ...] = (
    "main_checkout_lock",
    "main_checkout_guard",
    "local_dev_fast_forward",
    "push",
    "build",
    "build_churn",
    "tests",
    "ancestry_assertion",
    "worktree_placement",
    "diff",
)


def _failing_step_reason(step: str, entry: Any) -> Any | None:
    """Extract the reason code of a failing *entry*, or ``None`` when pass.

    Each ``finish`` step records its outcome in a different shape;
    this maps every shape onto a stable ``(step, reason)`` pair so the
    classifier never parses ``report["message"]`` prose
    (LP-0MVWEP1MM0097RAT AC5).
    """
    if step == "tests":
        # The tests step is a list of per-attempt gate records.
        attempts = entry if isinstance(entry, list) else []
        if any(
            isinstance(a, dict) and a.get("success") is False
            for a in attempts
        ):
            return "tests_failed"
        return None
    if not isinstance(entry, dict):
        return None
    if step == "main_checkout_lock":
        return "lock_held" if entry.get("acquired") is False else None
    if step == "main_checkout_guard":
        return "fail" if entry.get("verdict") == "fail" else None
    if step == "local_dev_fast_forward":
        if entry.get("method") == "refused":
            return entry.get("reason") or "refused"
        return None
    if step == "diff":
        if entry.get("success") is False:
            return entry.get("reason") or "no_changes"
        return None
    # Generic shape: build / push / ancestry_assertion / worktree_placement.
    if entry.get("success") is False:
        return entry.get("reason") or f"{step}_failed"
    return None


def _first_failing_step(steps: Any) -> tuple[str, Any] | None:
    """Return the first failing ``(step, reason)`` in precedence order.

    A step "fails" when its structured entry records a non-passing outcome
    (``acquired: False`` for the lock, ``verdict: "fail"`` for the guard,
    ``method: "refused"`` for the fast-forward, ``success: False`` for the
    rest). Returns ``None`` when no precedence step fails — the report then
    classifies as ``unknown`` (fail-safe).
    """
    if not isinstance(steps, dict):
        return None
    for step in FINISH_STEP_PRECEDENCE:
        entry = steps.get(step)
        if entry is None:
            continue
        reason = _failing_step_reason(step, entry)
        if reason is not None:
            return step, reason
    return None


def _finish_failure_kind(step: str, reason: Any) -> str:
    """Map a resolved ``(step, reason)`` onto a failure kind.

    Allow-list driven: ``infrastructure`` for an explicit infra allow-list
    hit, ``gate`` for an explicit gate-list hit (a wildcard reason matches
    any reason under the step), otherwise ``unknown`` — an unlisted pair is
    never ``infrastructure`` (parent epic LP-0MVVYQ1U3006GDWT AC1).
    """
    pair = (step, str(reason))
    if pair in FINISH_INFRA_REFUSALS:
        return "infrastructure"
    if pair in FINISH_GATE_REFUSALS or (
        step, FINISH_REASON_WILDCARD,
    ) in FINISH_GATE_REFUSALS:
        return "gate"
    return "unknown"


def classify_finish_failure(
    finish_report: dict[str, Any] | None,
) -> dict[str, Any]:
    """Classify a failing ``finish`` subprocess report into a failure kind.

    The single, table-driven classifier for ``finish`` failures (parent epic
    LP-0MVVYQ1U3006GDWT AC1). It turns the structured ``steps`` payload of
    a failed ``finish`` report into ``{kind, step, reason, message}`` so
    ``phase_drive`` can tell a transient infrastructure refusal from a
    genuine gate failure without excavating subprocess stdout.

    Resolution rules:

    * ``(step, reason)`` is resolved from the report's structured ``steps``
      entries in :data:`FINISH_STEP_PRECEDENCE` order — never from
      ``report["message"]`` prose;
    * ``kind == "infrastructure"`` only for an explicit allow-list hit in
      :data:`FINISH_INFRA_REFUSALS`;
    * ``kind == "gate"`` only for an explicit :data:`FINISH_GATE_REFUSALS`
      hit (a ``("step", "*")`` entry is a wildcard matching any reason
      under that step);
    * anything else — including ``None``, ``{}`` and non-dict input — is
      ``kind == "unknown"`` (fail-safe: ``unknown`` is never
      ``infrastructure``, so drive can never retry a genuine gate failure
      as infrastructure).

    Args:
        finish_report: The parsed ``finish`` subprocess JSON report, or
            ``None`` when no report could be parsed.

    Returns:
        ``{"kind", "step", "reason", "message", "steps"}`` where
        ``step``/``reason`` are ``None`` when no structured failing step
        resolves and ``steps`` is the failing report's raw ``steps`` payload
        (so a consumer can surface the main-checkout lock holder and guard
        evidence — LP-0MVWEP37K000JU0O AC2).
    """
    fail_safe: dict[str, Any] = {
        "kind": "unknown",
        "step": None,
        "reason": None,
        "message": None,
        "steps": None,
    }
    if not isinstance(finish_report, dict):
        return dict(fail_safe)
    message = finish_report.get("message")
    resolved = _first_failing_step(finish_report.get("steps"))
    if resolved is None:
        return {**fail_safe, "message": message}
    step, reason = resolved
    return {
        "kind": _finish_failure_kind(step, reason),
        "step": step,
        "reason": reason,
        "message": message,
        "steps": finish_report.get("steps"),
    }


def _fast_forward_local_dev_for_push(
    repo_root: str,
    commit_hash: str,
    parent_branch: str = DEFAULT_PARENT_BRANCH,
) -> dict[str, Any]:
    """Advance the main checkout's local parent branch to *commit_hash*.

    This fast-forward is **main-checkout hygiene only**: the dev-push gate's
    subject is the pushed commit (resolved from the worktree that holds it),
    so it does not measure this checkout. The advance merely keeps the main
    checkout's local branch up to date so the next worktree forks from a
    current base and the checkout does not lag ``dev`` (the historical reason
    — a pre-push gate that measured the main checkout — no longer applies; see
    SA-0MVTFMB7K002Y1F3). It is still made **loud and reversible**:

    * local ``dev`` is only ever advanced with ``git merge --ff-only`` — its
      history is never rewritten;
    * the pre-advance tip is returned as ``pre_advance_tip`` so a failed push
      can restore local ``dev`` to ``refs/remotes/origin/<parent_branch>``
      (:func:`_restore_local_dev_after_failed_push`);
    * when the advance is impossible — local ``dev`` has diverged from the
      remote tip, or the commit is not based on the current remote tip (a
      sibling landed first) — the helper **refuses loudly**, naming the refs
      and hashes, and leaves local ``dev`` at ``origin/<parent_branch>``;
    * it never fails the finish: the push (with its rebase-and-retry
      recovery) and the post-push ancestry assertion
      (:func:`_assert_commit_on_origin_dev`) remain authoritative.

    ``own_churn`` is recorded **only** when the advance happened
    (``method == "advanced"``): a refused or no-op fast-forward mutated nothing,
    so it contributes no attributable churn. Paths the commit diff claims but
    that were not observed dirty in this checkout are surfaced as
    ``not_observed`` — visible to the operator, never allow-listed
    (SA-0MVXVC03O0034A7V).

    Args:
        repo_root: Absolute path to the main checkout.
        commit_hash: The commit finish is about to push.
        parent_branch: The shared parent branch (default: ``dev``).

    Returns:
        Dict with ``method`` (``advanced`` | ``refused``), ``success``
        (always ``True`` — a refusal never fails a finish), ``advanced``,
        ``advanced_to`` / ``pre_advance_tip`` when advanced, ``local_tip``,
        ``remote_tip``, ``own_churn`` and, on refusal, ``reason`` +
        ``warning``.
    """
    result: dict[str, Any] = {
        "method": "none",
        "success": True,
        "advanced": False,
        "commit": commit_hash,
        "own_churn": [],
    }
    remote_ref = f"refs/remotes/origin/{parent_branch}"

    def _refuse(reason: str, warning: str) -> dict[str, Any]:
        result["method"] = "refused"
        result["reason"] = reason
        result["warning"] = warning
        LOG.warning("%s", warning)
        return result

    # 0. Capture the pre-fast-forward HEAD so the main-checkout guard can
    #    attribute the fast-forward's own file churn.
    before_head_result = run_cmd(["git", "rev-parse", "HEAD"], cwd=repo_root)
    before_head = (
        before_head_result.stdout.strip()
        if before_head_result.returncode == 0
        else ""
    )

    # 1. Refresh the remote-tracking ref the advance is measured against.
    #    Without it the remote tip cannot be verified, so no advance happens.
    fetch_result = run_cmd(
        ["git", "fetch", "origin", parent_branch],
        cwd=repo_root,
        check=False,
        timeout=120,
    )
    if fetch_result.returncode != 0:
        return _refuse(
            "fetch_failed",
            f"Refusing to advance local {parent_branch}: git fetch origin "
            f"{parent_branch} failed, so the remote tip cannot be verified "
            f"({fetch_result.stderr.strip()[:200]}). The push proceeds from "
            f"the current checkout; commit {commit_hash} is verified against "
            f"{remote_ref} after the push.",
        )
    remote_tip = _resolve_origin_parent_tip(repo_root, parent_branch)
    result["remote_tip"] = remote_tip
    if not remote_tip:
        return _refuse(
            "no_remote_tracking_ref",
            f"Refusing to advance local {parent_branch}: {remote_ref} does "
            f"not resolve after a successful fetch. The push proceeds; "
            f"commit {commit_hash} is verified after it.",
        )

    # 2. Ensure the main checkout is on the parent branch. This is hygiene
    #    only: the dev-push gate's subject is the pushed commit (resolved from
    #    the worktree), never this checkout, so the fast-forward just keeps the
    #    main checkout current for the next worktree.
    checkout_result = run_cmd(
        ["git", "checkout", parent_branch], cwd=repo_root, check=False
    )
    if checkout_result.returncode != 0:
        return _refuse(
            "checkout_failed",
            f"Refusing to advance local {parent_branch}: git checkout "
            f"{parent_branch} failed in {repo_root} "
            f"({checkout_result.stderr.strip()[:200]}). The push proceeds "
            f"from the current checkout; commit {commit_hash} is verified "
            f"after the push.",
        )
    local_tip = _resolve_full_commit(repo_root, f"refs/heads/{parent_branch}")
    result["local_tip"] = local_tip

    # 2b. A local branch that already carries commits origin/dev does not have
    #     has moved since this worktree's base. Merging the remote ref into it
    #     would be a silent no-op (the remote tip is an ancestor), leaving the
    #     shared branch ahead of the remote and making the finish commit
    #     impossible to fast-forward onto — so refuse loudly *before*
    #     touching anything (AC1).
    if local_tip and remote_tip and local_tip != remote_tip:
        local_ahead_result = run_cmd(
            ["git", "merge-base", "--is-ancestor", remote_tip, local_tip],
            cwd=repo_root,
            check=False,
        )
        if local_ahead_result.returncode == 0:
            return _refuse(
                "local_dev_diverged",
                f"Refusing to advance local {parent_branch}: it has moved "
                f"since the worktree base and carries commits {remote_ref} "
                f"does not have (local={local_tip}, origin={remote_tip}). "
                f"Local {parent_branch} is left untouched; commit "
                f"{commit_hash} is pushed from the feature branch and "
                f"verified against {remote_ref} after the push.",
            )

    # 3. Fast-forward local dev to origin/dev only (never rewrite history).
    merge_origin_result = run_cmd(
        ["git", "merge", "--ff-only", remote_ref], cwd=repo_root, check=False
    )
    if merge_origin_result.returncode != 0:
        return _refuse(
            "local_dev_diverged",
            f"Refusing to advance local {parent_branch}: it has moved since "
            f"the worktree base and cannot be fast-forwarded to "
            f"{remote_ref} (local={local_tip}, origin={remote_tip}) "
            f"({merge_origin_result.stderr.strip()[:200]}). Local "
            f"{parent_branch} is left untouched; commit {commit_hash} is "
            f"pushed from the feature branch and verified against "
            f"{remote_ref} after the push.",
        )

    # 4. Fast-forward local dev onto the finish commit.
    merge_commit_result = run_cmd(
        ["git", "merge", "--ff-only", commit_hash],
        cwd=repo_root,
        check=False,
    )
    if merge_commit_result.returncode != 0:
        return _refuse(
            "commit_not_based_on_remote_tip",
            f"Refusing to advance local {parent_branch} to {commit_hash}: the "
            f"commit is not a descendant of {remote_ref} ({remote_tip}) — "
            f"local {parent_branch} has moved since this worktree's base "
            f"({merge_commit_result.stderr.strip()[:200]}). Local "
            f"{parent_branch} stays at {remote_tip}; the push will land or "
            f"fail non-fast-forward and be rebased, and the post-push "
            f"ancestry assertion decides success.",
        )

    result["method"] = "advanced"
    result["advanced"] = True
    result["pre_advance_tip"] = local_tip
    result["advanced_to"] = _resolve_full_commit(repo_root, commit_hash)
    # The advance's own churn is expected and never aborts — but only paths
    # *observed dirty* in this checkout after the advance are attributable
    # (WL-0MVPADZ1Z000FOSG AC3/AC4, SA-0MVXVC03O0034A7V).
    not_observed: list[str] = []
    result["own_churn"] = _record_finish_own_churn(
        repo_root,
        before_head,
        commit_hash,
        not_observed=not_observed,
    )
    # Diagnostic surface: every path the diff named that could not be
    # attributed to this checkout. Visible, never allow-listed.
    result["not_observed"] = not_observed
    LOG.info(
        "Fast-forwarded local %s to %s (was %s)",
        parent_branch,
        commit_hash,
        local_tip,
    )
    return result


def _restore_local_dev_after_failed_push(
    repo_root: str,
    fast_forward: dict[str, Any] | None,
    parent_branch: str = DEFAULT_PARENT_BRANCH,
) -> dict[str, Any]:
    """Undo finish's own pre-push fast-forward after a failed push (AC1).

    Invariant: local ``dev`` must never be left pointing at an unpushed finish
    commit after a failed push. Otherwise the next child worktree forks from a
    base that ``origin/dev`` does not have and the commit is silently orphaned
    (the 2026-10-25 incident). The restore is deliberately conservative:

    * the target is ``refs/remotes/origin/<parent>`` — a commit known to be on
      the remote (never a locally-created commit);
    * only finish's **own** advance is undone: when the branch has moved since
      (another session, or the operator) nothing is touched and the refusal is
      reported loudly;
    * when the branch is checked out, ``git reset --merge`` is used so
      uncommitted work in the shared checkout survives — never
      ``reset --hard``; if a local modification overlaps the commit being
      undone the reset is skipped and reported loudly;
    * when the branch is not checked out, the ref is moved with an atomic
      compare-and-swap ``git update-ref``.

    Args:
        repo_root: Absolute path to the main checkout.
        fast_forward: The :func:`_fast_forward_local_dev_for_push` result.
        parent_branch: The shared parent branch (default: ``dev``).

    Returns:
        Dict recording what happened (``restored``, ``target``, ``reason``,
        hashes and an optional loud ``warning``).
    """
    result: dict[str, Any] = {
        "restored": False,
        "success": True,
        "target": f"refs/remotes/origin/{parent_branch}",
    }
    if not fast_forward or not fast_forward.get("advanced"):
        result["reason"] = "no_advance_to_undo"
        return result

    advanced_to = _resolve_full_commit(
        repo_root, str(fast_forward.get("advanced_to") or fast_forward.get("commit"))
    )
    current = _resolve_full_commit(repo_root, f"refs/heads/{parent_branch}")
    result["advanced_to"] = advanced_to
    result["current"] = current
    if advanced_to and current and current != advanced_to:
        result["reason"] = "branch_moved_since_advance"
        result["warning"] = (
            f"Not restoring local {parent_branch}: it moved from the tip this "
            f"finish advanced to ({advanced_to}) to {current}, so another "
            f"actor owns that move and the shared checkout is left untouched."
        )
        LOG.warning("%s", result["warning"])
        return result

    target_tip = _resolve_origin_parent_tip(repo_root, parent_branch)
    result["target_tip"] = target_tip
    if not target_tip:
        result["reason"] = "no_remote_tracking_ref"
        result["warning"] = (
            f"Cannot restore local {parent_branch}: {result['target']} does "
            f"not resolve. Local {parent_branch} may still point at the "
            f"unpushed commit {current or 'unknown'} — the next finish refuses "
            f"to advance it and reports the divergence."
        )
        LOG.warning("%s", result["warning"])
        return result
    if current == target_tip:
        result["restored"] = True
        result["reason"] = "already_at_remote_tip"
        return result

    head_result = run_cmd(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root, check=False
    )
    on_parent_branch = (
        head_result.returncode == 0
        and head_result.stdout.strip() == parent_branch
    )
    if on_parent_branch:
        # ``--merge`` resets the index/worktree for the files our own advance
        # touched and keeps every other uncommitted change; it aborts rather
        # than clobber a conflicting local modification (never ``--hard``).
        reset_result = run_cmd(
            ["git", "reset", "--merge", target_tip],
            cwd=repo_root,
            check=False,
        )
        if reset_result.returncode != 0:
            result["reason"] = "reset_conflict"
            result["warning"] = (
                f"Cannot restore local {parent_branch} to {target_tip}: "
                f"uncommitted changes in {repo_root} overlap the commit being "
                f"undone ({reset_result.stderr.strip()[:200]}). Nothing was "
                f"reset — the shared checkout keeps its uncommitted work; "
                f"local {parent_branch} still points at {current}. Restore "
                f"manually: git -C {repo_root} reset --merge {target_tip}"
            )
            LOG.warning("%s", result["warning"])
            return result
    else:
        # Not checked out here: move the ref only, with an atomic
        # compare-and-swap on the tip we created.
        update_result = run_cmd(
            [
                "git",
                "update-ref",
                f"refs/heads/{parent_branch}",
                target_tip,
                current or "",
            ],
            cwd=repo_root,
            check=False,
        )
        if update_result.returncode != 0:
            result["reason"] = "update_ref_failed"
            result["warning"] = (
                f"Cannot restore local {parent_branch} to {target_tip}: "
                f"git update-ref failed "
                f"({update_result.stderr.strip()[:200]}). Local "
                f"{parent_branch} still points at {current}."
            )
            LOG.warning("%s", result["warning"])
            return result

    result["restored"] = True
    LOG.info(
        "Restored local %s to %s after the failed push (was %s)",
        parent_branch,
        target_tip,
        current,
    )
    return result


def _assert_commit_on_origin_dev(
    repo_root: str,
    commit_hash: str,
    parent_branch: str = DEFAULT_PARENT_BRANCH,
    branch: str | None = None,
    worktree_path: str | None = None,
) -> dict[str, Any]:
    """Assert finish's own commit is on the pushed ``origin/<parent>`` (AC2).

    The assertion fails **closed**: ``finish`` may only report success when
    ``git merge-base --is-ancestor <commit> refs/remotes/origin/<parent>``
    positively holds. A commit that was not published — including a rebased
    replacement hash produced by non-fast-forward recovery — therefore can
    never be reported as success (the 2026-10-25 orphan's false positive).

    The remote-tracking ref is refreshed first (best effort: a push already
    updates it, a fetch just makes the verdict independent of that).

    Args:
        repo_root: Absolute path to the main checkout.
        commit_hash: The commit finish pushed (short or full hash).
        parent_branch: The shared parent branch (default: ``dev``).
        branch: Feature branch, for the refusal message only.
        worktree_path: Retained worktree, for the refusal message only.

    Returns:
        Dict with ``verified``, ``commit``, ``resolved_commit``, ``target``,
        ``origin_tip`` and ``fetched``.

    Raises:
        AncestryAssertionError: when the commit is not an ancestor of the
            remote-tracking parent ref, or the check cannot be performed.
    """
    target_ref = f"refs/remotes/origin/{parent_branch}"
    result: dict[str, Any] = {
        "verified": False,
        "commit": commit_hash,
        "target": target_ref,
        "branch": branch,
        "worktree_path": worktree_path,
    }
    fetch_result = run_cmd(
        ["git", "fetch", "origin", parent_branch],
        cwd=repo_root,
        check=False,
        timeout=120,
    )
    result["fetched"] = fetch_result.returncode == 0

    resolved = _resolve_full_commit(repo_root, commit_hash)
    origin_tip = _resolve_origin_parent_tip(repo_root, parent_branch)
    result["resolved_commit"] = resolved
    result["origin_tip"] = origin_tip
    if not resolved or not origin_tip:
        raise AncestryAssertionError(
            commit_hash,
            resolved,
            target_ref,
            origin_tip,
            "the commit or the remote-tracking ref could not be resolved, so "
            "publication cannot be verified",
        )

    ancestor_result = run_cmd(
        ["git", "merge-base", "--is-ancestor", resolved, target_ref],
        cwd=repo_root,
        check=False,
    )
    if ancestor_result.returncode != 0:
        raise AncestryAssertionError(
            commit_hash,
            resolved,
            target_ref,
            origin_tip,
            "git merge-base --is-ancestor reports the commit is not on the "
            "remote branch",
        )

    result["verified"] = True
    LOG.info(
        "Ancestry verified: %s is on %s (%s)", resolved, target_ref, origin_tip
    )
    return result


# ---------------------------------------------------------------------------
# Process cleanup helpers
# ---------------------------------------------------------------------------


def _main_checkout_for(worktree_path: str) -> str:
    """Resolve a cwd guaranteed to be OUTSIDE the given worktree.

    ``wl cleanup-worktree`` must not be spawned from inside the worktree it
    targets: ContextHub's ``wl`` CLI auto-registers its own PID under a
    worktree whenever its cwd is inside ``.worklog/worktrees/`` and then
    reaps it — including the registering process itself
    (SA-0MVJC5KNT0044L2B). Prefer the git-derived main checkout root, then
    the canonical ``<main>/.worklog/worktrees/<slug>`` layout, and finally
    the system temp dir (neutral — can never be inside a managed worktree).

    Args:
        worktree_path: Absolute path to the worktree directory.

    Returns:
        An absolute directory path outside *worktree_path*.
    """
    try:
        git_main = _get_repo_root(worktree_path)
    except OSError:
        # The worktree path no longer exists (e.g. cleanup during abort
        # after removal): fall through to the neutral fallbacks.
        git_main = None
    if git_main:
        return git_main
    parts = Path(worktree_path).resolve().parts
    if len(parts) >= 3 and parts[-3] == ".worklog" and parts[-2] == "worktrees":
        return str(Path(*parts[:-3]))
    return tempfile.gettempdir()


def _ppid_of(pid: int) -> int | None:
    """Return the parent PID of *pid* (Linux ``/proc``), else None."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            data = fh.read()
        rparen = data.rfind(")")
        if rparen == -1:
            return None
        fields = data[rparen + 2:].split()
        return int(fields[1])  # field 4 overall == ppid
    except (OSError, ValueError, IndexError):
        return None


def _pid_is_invoker_related(pid: int) -> bool:
    """True when *pid* must never be signalled by the worktree cleanup.

    The cleanup runs *inside* the worktree being reaped (``implement.py
    finish`` uses the worktree as cwd), so excluding only ``os.getpid()``
    is insufficient: the invoking process shares its process group and
    ancestry with the wrapper shell it was launched from and with any
    subprocesses it spawned. Signalling any of those lands on the finish
    process itself, stranding the dev push (SA-0MVJC5KNT0044L2B).

    A PID is considered invoker-related when it is the invoking process
    itself, belongs to the invoking process's process group, or is an
    ancestor or descendant of it.
    """
    if pid == os.getpid():
        return True
    try:
        if os.getpgid(pid) == os.getpgrp():
            return True
    except OSError:
        pass  # Concurrently-exited process; the individual kill will ESRCH.
    # Ancestry walk (bounded against pid-reuse loops): ancestors (a wrapper
    # shell even if reparented into another group) and descendants we
    # spawned (even if they moved to their own session, e.g. the push) both
    # resolve to us.
    seen: set[int] = set()
    current = pid
    while current not in seen and len(seen) < 64:
        seen.add(current)
        parent = _ppid_of(current)
        if parent is None or parent <= 1 or parent == current:
            break
        if parent == os.getpid():
            return True
        current = parent
    return False


def cleanup_worktree_processes(worktree_path: str) -> dict[str, Any]:
    """Terminate processes associated with the given worktree path.

    Attempts ``wl cleanup-worktree <path>`` first. Falls back to finding
    and killing processes with the worktree path in their cwd.

    **Self-exclusion contract (SA-0MVJC5KNT0044L2B).** This function may be
    invoked *from inside* the worktree it targets (``implement.py finish``
    runs with the worktree as cwd). The cleanup must therefore never signal
    the invoking process, its wrapper shell, or anything in its process
    group — a SIGTERM landing on the finish process mid-workflow previously
    reset the item to ``open`` and stranded the dev push. Three independent
    guards enforce this:

    1. ``wl cleanup-worktree`` is spawned with ``cwd`` outside the worktree
       (``_main_checkout_for``) so ContextHub's ``wl`` CLI does not
       auto-register the invoking tree's own PID (``cli.js`` /
       ``process-lifecycle.js`` register any process whose cwd is inside
       ``.worklog/worktrees/`` and then reap it — including the registering
       process itself); the kill set then only contains genuinely stray
       PIDs registered by other sessions.
    2. The ``wl`` subprocess runs in its own session (``start_new_session``)
       so even a register-then-kill inside it can only reach its own
       session/group, never the caller's.
    3. The pgrep fallback excludes the invoking process's whole process
       group and its ancestor/descendant chain — not merely
       ``os.getpid()`` — via ``_pid_is_invoker_related``.

    Args:
        worktree_path: Absolute path to the worktree directory.

    Returns:
        A dict with ``method`` (str), ``terminated`` (int), ``warning`` (str).
    """
    result: dict[str, Any] = {
        "method": "none",
        "terminated": 0,
        "warning": "",
    }

    abs_path = str(Path(worktree_path).resolve())

    # Try wl cleanup-worktree first — from a cwd OUTSIDE the worktree and in
    # its own session (guards 1 & 2 above). ``--json`` is required: without
    # it the CLI prints human text and the kill count below would always
    # fall through to the assume-success branch.
    wl_result = run_cmd(
        ["wl", "cleanup-worktree", abs_path, "--json"],
        cwd=_main_checkout_for(worktree_path),
        start_new_session=True,
        check=False,
        timeout=30,
    )
    if wl_result.returncode == 0:
        result["method"] = "wl_cleanup_worktree"
        try:
            data = json.loads(wl_result.stdout.strip())
            # cleanup-worktree.js reports the kill count as
            # ``processesKilled`` (single path) / ``worktreesCleaned``
            # (``--all``); tolerate the legacy ``terminated`` key.
            result["terminated"] = data.get(
                "processesKilled",
                data.get("terminated", data.get("worktreesCleaned", 0)),
            )
            result["warning"] = data.get("warning", "")
        except (json.JSONDecodeError, ValueError):
            result["terminated"] = 1  # Assume success if exit code is 0
        return result

    # If wl cleanup-worktree is unavailable (exit code != 0), try pgrep fallback
    LOG.warning(
        "wl cleanup-worktree unavailable (exit %d: %s); "
        "falling back to process scan",
        wl_result.returncode,
        wl_result.stderr.strip() or "command not found",
    )
    result["method"] = "pgrep_fallback"

    try:
        # Find processes with cwd matching the worktree path
        pgrep_result = run_cmd(
            ["pgrep", "-f", abs_path],
            check=False,
            timeout=15,
            capture=True,
        )
        if pgrep_result.returncode == 0 and pgrep_result.stdout.strip():
            pids = [
                int(pid) for pid in pgrep_result.stdout.strip().split()
                if pid.isdigit()
            ]
            # Guard 3: exclude the invoking process, its whole process group
            # and its ancestor/descendant chain — not merely ``os.getpid()``
            # (the wrapper shell the finish was launched from is frequently
            # exec'd into the finish process itself).
            targets = [
                pid for pid in pids if not _pid_is_invoker_related(pid)
            ]
            for pid in targets:
                try:
                    os.kill(pid, signal.SIGTERM)
                    result["terminated"] += 1
                except (OSError, PermissionError):
                    pass

            # Give processes time to terminate
            if result["terminated"] > 0:
                time.sleep(2)

            # Force kill any remaining
            for pid in targets:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (OSError, PermissionError):
                    pass

            result["warning"] = (
                "wl cleanup-worktree unavailable; used pgrep fallback. "
                "Result may be incomplete."
            )
        else:
            result["warning"] = "wl cleanup-worktree unavailable; no processes found via pgrep"
    except (FileNotFoundError, OSError) as exc:
        result["warning"] = (
            f"wl cleanup-worktree unavailable and pgrep fallback failed: {exc}"
        )

    return result


# ---------------------------------------------------------------------------
# Build helpers
# ---------------------------------------------------------------------------


def _package_script(cwd: str, name: str) -> Any:
    """Return the raw ``scripts.<name>`` value from the root package.json.

    Reads the root ``package.json`` directly so the check works without npm.
    Malformed JSON, a non-object ``scripts`` value, or a missing file counts
    as "no script" (fail-open — never block finish on a broken manifest).

    Args:
        cwd: Working directory (worktree root).
        name: Script name, e.g. ``build`` or ``test``.

    Returns:
        The raw ``scripts.<name>`` value, or None when the entry (or a
        readable package.json) does not exist.
    """
    package_json = Path(cwd) / "package.json"
    if not package_json.exists():
        return None
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return None
    return scripts.get(name)


def _has_build_script(cwd: str) -> bool:
    """Check whether the repo root package.json defines a ``build`` script.

    A repo without a ``scripts.build`` entry (e.g. Python-only projects) has
    nothing to build: ``npm run build`` would exit 1 with
    ``Missing script: "build"`` and block the finish phase for every
    implementation. Reads the root ``package.json`` directly so the check
    works without npm.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        True if the root package.json exists and contains a non-empty
        ``scripts.build`` entry. Malformed JSON or a non-object ``scripts``
        value counts as "no build script" (fail-open — never block finish on
        a broken manifest).
    """
    return bool(_package_script(cwd, "build"))


def _has_test_script(cwd: str) -> bool:
    """Check whether the repo root package.json defines a ``test`` script.

    Mirrors :func:`_has_build_script` for the test step: a repo without a
    ``scripts.test`` entry has no npm test suite — ``npm test`` would exit 1
    with ``Missing script: "test"``.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        True if the root package.json exists and contains a non-empty
        ``scripts.test`` entry.
    """
    return bool(_package_script(cwd, "test"))


def run_build(cwd: str) -> dict[str, Any]:
    """Run the project build script.

    Repos whose root package.json has no ``build`` script (e.g. Python-only
    projects) skip the build step: it is reported as a no-op with
    ``success: True`` so the finish phase proceeds to tests → commit → push
    instead of aborting. Repos WITH a build script run ``npm run build``
    unchanged — a real build failure still blocks finish.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        A dict with ``success`` (bool), ``stdout`` (str), ``stderr`` (str),
        ``exit_code`` (int), and ``skipped`` (bool) — ``skipped`` is True
        when the build step was bypassed because no build script exists.
    """
    if not _has_build_script(cwd):
        msg = "No build script in package.json — skipping build step (no-op)."
        LOG.info(msg)
        return {
            "success": True,
            "stdout": msg,
            "stderr": "",
            "exit_code": 0,
            "skipped": True,
        }
    # Inject the resolved disk-backed TMPDIR into the build child env so the
    # build (and anything it spawns) never lands on the inode-starved tmpfs.
    # Fail-open: a resolution failure leaves the ambient env unchanged and the
    # build proceeds (SA-0MW3ENQY0001RBSW AC2/AC4).
    build_env = None
    _finish_tmpdir = _resolved_finish_tmpdir()
    if _finish_tmpdir is not None:
        build_env = {"TMPDIR": _finish_tmpdir}
    result = run_cmd(
        ["npm", "run", "build"],
        cwd=cwd,
        check=False,
        timeout=300,
        capture=True,
        env=build_env,
    )
    return {
        "success": result.returncode == 0,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "exit_code": result.returncode,
        "skipped": False,
    }


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_PYTEST_CONFIG_MARKERS = (
    ("pyproject.toml", "[tool.pytest.ini_options]"),
    ("setup.cfg", "[tool:pytest]"),
    ("tox.ini", "[pytest]"),
)


def _pytest_importable(cwd: str) -> bool:
    """Whether ``python3 -m pytest`` would work in *cwd*.

    Probes with the same interpreter that runs the suite (``python3``) so
    venv/global-path differences are respected — ``shutil.which`` alone is
    not enough because pytest is commonly only available as a module inside
    a project venv.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        True when ``python3 -c "import pytest"`` exits 0.
    """
    try:
        probe = run_cmd(
            ["python3", "-c", "import pytest"],
            cwd=cwd,
            check=False,
            timeout=60,
            capture=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def _has_pytest_markers(cwd: str) -> bool:
    """Whether the repo declares pytest configuration.

    A ``pytest.ini`` file alone counts; for the other config files the
    relevant section must be present (``[tool.pytest.ini_options]`` in
    pyproject.toml, ``[tool:pytest]`` in setup.cfg, ``[pytest]`` in tox.ini)
    so an unrelated config file does not trigger a pytest run.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        True when the repo declares pytest configuration.
    """
    root = Path(cwd)
    if (root / "pytest.ini").is_file():
        return True
    for name, marker in _PYTEST_CONFIG_MARKERS:
        path = root / name
        if not path.is_file():
            continue
        try:
            if marker in path.read_text(encoding="utf-8"):
                return True
        except OSError:
            continue
    return False


def _has_pytest_test_files(cwd: str) -> bool:
    """Whether the repo contains pytest-style test files.

    Checks a bounded set of conventional locations (``tests/`` and ``test/``
    dirs, plus root/``src`` ``test_*.py`` / ``*_test.py`` globs) so the scan
    never walks into node_modules or other vendored trees.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        True when the repo contains Python test files.
    """
    root = Path(cwd)
    for dirname in ("tests", "test"):
        test_dir = root / dirname
        if test_dir.is_dir() and any(test_dir.rglob("*.py")):
            return True
    for base in (root, root / "src"):
        if base.is_dir() and (
            any(base.glob("test_*.py")) or any(base.glob("*_test.py"))
        ):
            return True
    return False


def _has_pytest_suite(cwd: str) -> bool:
    """Whether the repo has a runnable pytest suite.

    Both conditions must hold: pytest must be importable via ``python3``
    (otherwise running it would fail with ModuleNotFoundError) AND the repo
    must declare pytest (config marker or test files). The second condition
    prevents false positives on hosts with a global pytest that would
    otherwise run on empty bash-only repos and block finish with pytest's
    "no tests ran" exit code.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        True when the repo has a runnable pytest suite.
    """
    return _pytest_importable(cwd) and (
        _has_pytest_markers(cwd) or _has_pytest_test_files(cwd)
    )


def _is_unity_project(cwd: str) -> bool:
    """Whether *cwd* looks like a Unity project root.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        True when an ``Assets/`` directory or
        ``ProjectSettings/ProjectVersion.txt`` exists.
    """
    root = Path(cwd)
    return (root / "Assets").is_dir() or (root / "ProjectSettings" / "ProjectVersion.txt").is_file()


def _find_repo_test_runner(cwd: str) -> str | None:
    """Locate a repo-local test runner script at the repo root.

    Unity repos (and other non-npm/non-pytest repos) commonly ship their own
    runner, e.g. ``run_tests.sh`` or ``run_unity_tests.bat``; when present it
    is executed instead of skipping the test step.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        The script file name, or None when no runner is present.
    """
    root = Path(cwd)
    for name in ("run_tests.sh", "run_unity_tests.sh", "run_unity_tests.bat"):
        if (root / name).is_file():
            return name
    return None


def _detect_test_tooling(cwd: str) -> str | None:
    """Detect the repo's test tooling, in precedence order.

    Args:
        cwd: Working directory (worktree root).

    Returns:
        ``pytest`` | ``npm`` | ``repo-script`` | ``unity``, or None when the
        repo has no test tooling at all.
    """
    if _has_pytest_suite(cwd):
        return "pytest"
    if _has_test_script(cwd):
        return "npm"
    if _find_repo_test_runner(cwd) is not None:
        return "repo-script"
    if _is_unity_project(cwd):
        return "unity"
    return None


def _skip_test_result(message: str, tooling: str | None,
                      scope: str = "full") -> dict[str, Any]:
    """Build a skipped/no-op test result.

    Args:
        message: Informative no-op message (also logged).
        tooling: The detected tooling, if any (e.g. ``unity``).
        scope: The scope this run represented (``full``/``changed``).

    Returns:
        A result dict with ``success: True`` so finish proceeds.
    """
    LOG.info(message)
    return {
        "success": True,
        "stdout": message,
        "stderr": "",
        "exit_code": 0,
        "failures": [],
        "skipped": True,
        "tooling": tooling,
        "scope": scope,
        "cached": False,
        "cache_entries": [],
    }


def _cache_entry_from_run(
    run: dict[str, Any], command: str
) -> dict[str, Any]:
    """Reportable metadata of the cache entry a verdict was served from.

    ``run`` is a ``run_cached`` result that was a cache hit. The mapping
    carries the entry's ``command``, ``completed_at`` (epoch float), ``scope``
    and ``fingerprint_kind`` so the finish report and the completion comment
    can state *what* was reused, not merely that something was
    (SA-0MVQHVJEX0071R7G).
    """
    return {
        "command": run.get("command") or command,
        "completed_at": run.get("completed_at", 0),
        "scope": run.get("scope", "full"),
        "fingerprint_kind": run.get("fingerprint_kind", ""),
    }


def _with_cache_provenance(
    result: dict[str, Any],
    *,
    cached: bool,
    entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Attach verdict provenance (cache flag + entry metadata) to *result*.

    ``cached`` is True only when the whole verdict was served from the cache;
    ``cache_entry`` then identifies the entry that was served (command,
    completion time, scope, fingerprint kind) so a partially-scoped or foreign
    entry can never masquerade as a fresh full-tree verdict
    (SA-0MVQHVJEX0071R7G).
    """
    result["cached"] = bool(cached)
    result["cache_entries"] = list(entries or [])
    if cached and result["cache_entries"]:
        result["cache_entry"] = result["cache_entries"][0]
    return result


def _finalize_test_result(result: dict[str, Any], tooling: str | None,
                          scope: str = "full") -> dict[str, Any]:
    """Add metadata keys and parse failures from a raw run result.

    Args:
        result: Raw run dict (stdout/stderr/exit_code).
        tooling: The runner that produced the result.
        scope: The scope this run represented (``full``/``changed``).

    Returns:
        The result dict with ``success``, ``failures``, ``skipped: False``,
        ``tooling`` and ``scope`` keys.
    """
    failures: list[str] = []
    combined = f"{result['stdout']}\n{result['stderr']}"
    for line in combined.splitlines():
        if "FAILED" in line or "failed" in line.lower():
            failures.append(line.strip())
    return {
        "success": result["exit_code"] == 0,
        "stdout": result["stdout"].strip(),
        "stderr": result["stderr"].strip(),
        "exit_code": result["exit_code"],
        "failures": failures,
        "skipped": False,
        "tooling": tooling,
        "scope": scope,
    }


def _shell_command_runner(
    command: str, cwd: str, timeout: int
) -> subprocess.CompletedProcess:
    """Run a shell command string through bash (per-repo overrides/runners)."""
    return run_cmd(
        ["bash", "-c", command],
        cwd=cwd,
        check=False,
        timeout=timeout,
        capture=True,
        keep_git_index=True,
    )


def _log_test_slot_wait(queued_at: float, wait_seconds: float) -> None:
    """Emit implement-gate pacing telemetry (SA-0MUA8BSAG000YZA2 AC3).

    Called by the test skill's pacer once a "test" slot is held and just
    before the command is spawned, so a contended host produces an
    observable ``queued_at`` / ``wait_seconds`` line instead of silently
    colliding.
    """
    LOG.info(
        "Implement run_tests: test slot acquired — queued_at=%.3f "
        "wait_seconds=%.3f",
        queued_at,
        wait_seconds,
    )


def _paced_runner(runner: Any) -> Any:
    """Route a cache *runner* through the test skill's host-wide pacer.

    Wrapping the runner (not ``run_cached`` itself) means only ACTUAL
    executions acquire a ``"test"`` semaphore slot; a cache hit never
    invokes the runner and so never consumes a slot
    (SA-0MUKHCO02009EQFG AC1/AC2). The pacer also emits a
    ``queued_at`` / ``wait_seconds`` telemetry line after acquiring the slot
    (SA-0MUA8BSAG000YZA2 AC3). Falls back to the unmodified runner on
    partial skill installs where the test skill is unavailable (degraded,
    unpaced execution rather than failing the gate outright).
    """
    if _test_paced_runner is None:
        return runner
    return _test_paced_runner(runner, on_wait=_log_test_slot_wait)


def _run_cached_paced(command: str, **kwargs: Any) -> dict[str, Any]:
    """``run_cached`` with the runner paced through the test semaphore.

    On ``TEST_LOCK_TIMEOUT`` saturation, returns a clear failed result (the
    exception path stores nothing in the cache, so a saturated host never
    poisons the cache with a phantom timeout entry). Mirrors the test
    skill's bounded-wait semantics (SA-0MTG5U75A001F1RG,
    SA-0MUKHCO02009EQFG AC3).
    """
    runner = kwargs.pop("runner", None)
    if runner is not None:
        kwargs["runner"] = _paced_runner(runner)
    try:
        return run_cached(command, **kwargs)
    except _TestConcurrencyTimeout as exc:
        LOG.error(
            "Implement run_tests: could not acquire a test-run slot — %s. "
            "Another suite is executing; wait a STAGGERED delay (e.g. "
            "30-120s, do NOT re-run immediately — concurrent waiters re-enter "
            "the pool in lockstep otherwise), or raise TEST_LOCK_TIMEOUT to "
            "wait longer.",
            exc,
        )
        return {
            "stdout": "",
            "stderr": str(exc),
            "exit_code": 1,
            "command": command,
            "git_state": "",
            "cached": False,
        }


def _guarded_exit_code(guarded: dict[str, Any]) -> int:
    """Extract the aggregate exit code from a ``guarded_run_all`` result.

    ``run_all`` aggregates per-suite results but does not expose a top-level
    ``returncode``; the first non-zero suite returncode is the run's exit
    code (0 when every suite passed). Falls back to ``success`` when no
    per-suite returncode is available.
    """
    suites = guarded.get("suites") or {}
    if isinstance(suites, dict):
        for result in suites.values():
            if not isinstance(result, dict):
                continue
            returncode = result.get("returncode")
            if returncode:
                return int(returncode)
    return 0 if guarded.get("success") else 1


#: Bound for the output tail carried into implement.py's gate diagnostics
#: (WL-0MVGUT787000LNDG): a failed suite can print megabytes; the operator
#: message only ever sees this tail.
GATE_OUTPUT_TAIL_CHARS = 1200


def _tail_text(text: str | None, max_chars: int = GATE_OUTPUT_TAIL_CHARS) -> str:
    """Return a bounded tail of *text* (last ``max_chars`` characters).

    Empty/None input yields an empty string; short text is returned whole.
    """
    if not text:
        return ""
    text = text.rstrip()
    if len(text) <= max_chars:
        return text
    return text[-max_chars:].lstrip()


def _guarded_output(guarded: dict[str, Any], key: str) -> str:
    """Aggregate the *key* output of a guarded run.

    Prefers the aggregate field (newer test skill); falls back to
    concatenating the per-suite fields so a legacy ``run_all`` shape still
    yields a diagnostic output tail (WL-0MVGUT787000LNDG).
    """
    direct = guarded.get(key)
    if isinstance(direct, str) and direct.strip():
        return direct
    parts: list[str] = []
    suites = guarded.get("suites")
    if isinstance(suites, dict):
        for suite in suites.values():
            if isinstance(suite, dict):
                value = suite.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
    return "\n".join(parts)


def _gate_facts(
    guarded: dict[str, Any], exit_code: int
) -> dict[str, Any]:
    """The infrastructure facts a ``guarded_run_all`` result carries.

    Read from the aggregate when the runner reports them there and otherwise
    from the first suite that has them, so a partial or older result still
    contributes whatever it measured. ``signal`` is derived from a negative
    exit code when the runner did not state it (a signal-terminated child is
    ``returncode < 0`` — SA-0MVU8XRBQ000DG9D).

    Args:
        guarded: The ``guarded_run_all`` result.
        exit_code: The resolved aggregate exit code.

    Returns:
        A mapping of the fact fields that are known (never ``None`` values, so
        an unknown fact is absent rather than misleading).
    """
    facts: dict[str, Any] = {}
    suites = guarded.get("suites") or {}
    suite_results = (
        [s for s in suites.values() if isinstance(s, dict)]
        if isinstance(suites, dict)
        else []
    )

    def _from_suites(field: str) -> Any:
        for suite in suite_results:
            value = suite.get(field)
            if value is not None:
                return value
        return None

    configured_timeout = guarded.get("configured_timeout")
    if configured_timeout is None:
        configured_timeout = _from_suites("configured_timeout")
    if configured_timeout is not None:
        facts["configured_timeout"] = configured_timeout

    elapsed = guarded.get("elapsed_seconds")
    if elapsed is None:
        elapsed = guarded.get("elapsed_seconds_total")
    if elapsed is None:
        elapsed = _from_suites("elapsed_seconds")
    if elapsed is not None:
        facts["elapsed_seconds"] = elapsed

    slot_wait = guarded.get("slot_wait_seconds")
    if slot_wait is None:
        slot_wait = _from_suites("slot_wait_seconds")
    if slot_wait is not None:
        facts["slot_wait_seconds"] = slot_wait

    command = guarded.get("command") or _from_suites("command")
    if command:
        facts["command"] = command

    signal_number = guarded.get("signal")
    if signal_number is None:
        signal_number = _from_suites("signal")
    if signal_number is None and exit_code < 0:
        signal_number = abs(exit_code)
    if signal_number is not None:
        facts["signal"] = signal_number

    # Only a suite that actually reported a non-zero code states an exit code:
    # the report-shape fallback of ``1`` must never be presented as one.
    if _reported_exit_code(guarded) is not None or exit_code < 0:
        facts["exit_code"] = exit_code
    return facts


def _adapt_guarded_result(
    guarded: dict[str, Any], tooling: str | None, scope: str
) -> dict[str, Any]:
    """Adapt a ``guarded_run_all`` result to implement.py's result contract.

    ``run_all`` reports structured failure dicts and an aggregate
    ``success``; implement.py's finish loop expects a flat ``failures`` list
    of strings, an ``exit_code``, ``skipped`` and ``tooling``. The
    ``live_repo_mutation`` key (set by the canonical outer guard) is carried
    through so a mutated checkout always fails the gate.

    The guarded run's diagnostics are carried through additive-only (never
    dropped): ``notices``/``notice_codes`` (slot contention, command-not-found,
    suite timeout) and bounded ``stdout``/``stderr`` tails, so ``phase_finish``
    can explain WHY the gate failed instead of emitting an empty ``Failures:``
    message (WL-0MVGUT787000LNDG).

    ``live_repo_excluded`` is carried too: working-tree churn attributed to a
    concurrent session never fails the gate, but it is surfaced in the finish
    output so an excluded foreign change is distinguishable from a clean run
    (SA-0MUIJAHXS003M7BG AC3).
    """
    failures: list[str] = []
    for failure in guarded.get("failures", []):
        if isinstance(failure, dict):
            failures.append(str(failure.get("test_name") or failure))
        else:
            failures.append(str(failure))
    notices = [
        str(n).strip()
        for n in guarded.get("notices", [])
        if str(n).strip()
    ]
    notice_codes = [
        str(c).strip()
        for c in guarded.get("notice_codes", [])
        if str(c).strip()
    ]
    state_changed_flags = guarded.get("state_changed_during_run") or []
    if isinstance(state_changed_flags, dict):
        state_changed_flags = [state_changed_flags]
    state_changed_flags = [
        f for f in state_changed_flags if isinstance(f, dict) and f
    ]
    exit_code = _guarded_exit_code(guarded)
    result: dict[str, Any] = {
        "success": bool(guarded.get("success")) and exit_code == 0,
        "stdout": _tail_text(_guarded_output(guarded, "stdout")),
        "stderr": _tail_text(_guarded_output(guarded, "stderr")),
        "exit_code": exit_code,
        # Whether the runner *reported* an exit code (as opposed to the
        # report-shape fallback) so the gate never presents a fabricated one.
        "exit_code_reported": _explicit_suite_exit_code(guarded) is not None,
        "failures": failures,
        "notices": notices,
        "notice_codes": notice_codes,
        # Moved-checkout flags (SA-0MW3FF18I003KGZE AC4): carried through
        # additively so the gate classification consults the flag before any
        # failure-based branch, and the operator message can name the checkout
        # movement and both commits.
        "state_changed_during_run": state_changed_flags,
        "skipped": False,
        "tooling": tooling,
        "scope": guarded.get("scope", scope),
    }
    mutation = guarded.get("live_repo_mutation")
    if mutation:
        result["live_repo_mutation"] = mutation
        result["success"] = False
    excluded = [
        str(n).strip()
        for n in guarded.get("live_repo_excluded", [])
        if str(n).strip()
    ]
    if excluded:
        result["live_repo_excluded"] = excluded
    # Infrastructure facts (SA-0MVTET8KQ0031R6Z): the runner states what
    # happened — configured timeout, execution time, queue wait, command, exit
    # code and signal — and the gate must not drop a single one, because they
    # are what decides the remedy. Additive only: absent keys stay absent.
    for field, value in _gate_facts(guarded, exit_code).items():
        result[field] = value
    # Verdict provenance (SA-0MVQHVJEX0071R7G): carry the cache flag and the
    # served entry's metadata through from the guarded run so the finish
    # report can attribute the verdict (fresh run vs. which cached entry).
    return _with_cache_provenance(
        result,
        cached=bool(guarded.get("cached", False)),
        entries=list(guarded.get("cache_entries") or []),
    )


#: Classification of a test-gate failure (WL-0MVGUT787000LNDG). ``phase_finish``
#: must distinguish a retryable infrastructure condition (no free test slot)
#: from a genuine code failure so the operator does not burn a blind retry.
#:
#: The three infrastructure kinds below answer the only question an operator
#: woken by a red gate has to settle before acting
#: (SA-0MVTET8KQ0031R6Z): fix code (``test_failure``), raise
#: ``timeoutPerCommand`` (``timeout``), treat the host as the problem
#: (``killed`` / ``slot_contention``), or read the output because the suite
#: said something the parser could not parse (``unparsed``).
GATE_FAILURE_SLOT_CONTENTION = "slot_contention"
GATE_FAILURE_INFRASTRUCTURE = "infrastructure"
GATE_FAILURE_TESTS = "test_failure"
GATE_FAILURE_TIMEOUT = "timeout"
GATE_FAILURE_KILLED = "killed"
GATE_FAILURE_UNPARSED = "unparsed"
GATE_FAILURE_UNKNOWN = "unknown"

#: Fact fields carried from the runner into the gate report and message. Every
#: one is additive: a consumer reading an older result simply finds no key.
GATE_FACT_FIELDS: tuple[str, ...] = (
    "configured_timeout",
    "elapsed_seconds",
    "slot_wait_seconds",
    "command",
    "exit_code",
    "signal",
)


def _explicit_suite_exit_code(test_result: dict[str, Any]) -> int | None:
    """The run's exit code from an *explicit* per-suite ``returncode``.

    Returns ``None`` when no suite reported one, so the classification can tell
    "a suite exited non-zero" apart from "the runner never ran a command" —
    the fallback ``1`` used for the report shape must never be read as a
    genuine non-zero exit.
    """
    suites = test_result.get("suites") or {}
    if not isinstance(suites, dict):
        return None
    for suite in suites.values():
        if not isinstance(suite, dict):
            continue
        returncode = suite.get("returncode")
        if isinstance(returncode, int) and returncode != 0:
            return returncode
    return None


def _reported_exit_code(test_result: dict[str, Any]) -> int | None:
    """The run's exit code as *reported*, from either result shape.

    A raw ``guarded_run_all`` result carries per-suite ``returncode``s; an
    adapted result carries the resolved ``exit_code`` plus
    ``exit_code_reported``. Both are read so the classification works on either
    side of the boundary, and a report-shape fallback is never mistaken for a
    genuine non-zero exit.
    """
    explicit = _explicit_suite_exit_code(test_result)
    if explicit is not None:
        return explicit
    if test_result.get("exit_code_reported"):
        exit_code = test_result.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            return exit_code
    return None


def _gate_failure_kind(test_result: dict[str, Any]) -> str:
    """Classify why the test gate failed.

    Genuine structured failures always win: a real test failure must never be
    reported as (or fixed by retrying instead of fixing) an infrastructure
    condition. Classification then proceeds on stable machine-readable codes
    (never on notice wording), with a narrow legacy fallback on the slot-busy
    notice prefix for a result produced by an older test skill (WL-0MVGUT787000LNDG).

    A timeout, a signal kill and an unparsable non-zero exit each get their own
    reason (SA-0MVTET8KQ0031R6Z). When the runner predates the ``killed`` /
    ``unparsed_failure`` codes the *shape* decides instead: a negative suite
    exit code is a signal termination and a positive one with no parsable
    failure is unparsed output. ``unknown`` therefore survives only for a
    failure with no structured failure, no notice and no executed command —
    never for a timed-out, killed or non-zero run.
    """
    if test_result.get("state_changed_during_run"):
        return GATE_FAILURE_INFRASTRUCTURE
    if test_result.get("failures"):
        return GATE_FAILURE_TESTS
    # Single shared classifier (SA-0MVSJX5HS002DJHI): the pre-push hook and
    # this gate must never fork the reason vocabulary. The local body below is
    # a compatibility fallback for an older test skill that predates the
    # shared module.
    if _classify_gate_result is not None:
        return _classify_gate_result(test_result)
    notice_codes = [str(c) for c in (test_result.get("notice_codes") or [])]
    if _NOTICE_SLOT_BUSY in notice_codes:
        return GATE_FAILURE_SLOT_CONTENTION
    for notice in test_result.get("notices") or []:
        if str(notice).startswith(_SLOT_BUSY_NOTICE_PREFIX):
            return GATE_FAILURE_SLOT_CONTENTION
    if test_result.get("live_repo_mutation"):
        return GATE_FAILURE_INFRASTRUCTURE
    if _NOTICE_SUITE_TIMEOUT in notice_codes:
        return GATE_FAILURE_TIMEOUT
    if _NOTICE_KILLED in notice_codes:
        return GATE_FAILURE_KILLED
    if _NOTICE_UNPARSED_FAILURE in notice_codes:
        return GATE_FAILURE_UNPARSED
    exit_code = _reported_exit_code(test_result)
    if exit_code is not None:
        if exit_code < 0:
            return GATE_FAILURE_KILLED
        return GATE_FAILURE_UNPARSED
    if notice_codes or test_result.get("notices"):
        return GATE_FAILURE_INFRASTRUCTURE
    return GATE_FAILURE_UNKNOWN


def _gate_failure_detail(test_result: dict[str, Any]) -> str:
    """Render the root-cause detail for a failed test gate.

    Never returns an empty string (WL-0MVGUT787000LNDG): structured failures
    first, then runner notices (slot contention / command-not-found / suite
    timeout), then the command output tail (unparsed vitest/node failures),
    and finally an explicit "no diagnostics" message so the operator is never
    shown a blank ``Failures:`` block.
    """
    failures = [str(f) for f in (test_result.get("failures") or [])]
    if failures:
        return "Failures:\n" + "\n".join(failures[:20])

    state_changed_flags = test_result.get("state_changed_during_run") or []
    if isinstance(state_changed_flags, dict):
        state_changed_flags = [state_changed_flags]
    if state_changed_flags:
        movement = "\n".join(
            "- " + _state_changed_detail(f)
            for f in state_changed_flags
            if isinstance(f, dict)
        )
        sections: list[str] = [
            "Checkout moved during the run (infrastructure, not a code "
            "failure); the result was discarded:\n" + movement
        ]
        notices = [
            str(n).strip()
            for n in (test_result.get("notices") or [])
            if str(n).strip()
        ]
        if notices:
            # A more specific gate notice (e.g. "gate evidence invalid: the
            # temporary index changed") stays visible: the movement explains
            # the symptom, the notice names the mechanism.
            sections.append("Runner notices:\n" + "\n".join(notices))
        return "\n\n".join(sections)

    sections: list[str] = []
    notices = [
        str(n).strip()
        for n in (test_result.get("notices") or [])
        if str(n).strip()
    ]
    if notices:
        sections.append("Runner notices:\n" + "\n".join(notices))
    mutation = test_result.get("live_repo_mutation")
    if mutation:
        sections.append(
            "Live-repo mutation detected:\n" + _tail_text(str(mutation))
        )
    facts = _render_gate_facts(_gate_facts(test_result, _gate_exit_code(test_result)))
    if facts:
        sections.append("Gate facts:\n" + facts)
    output_tail = _tail_text(test_result.get("stderr")) or _tail_text(
        test_result.get("stdout")
    )
    if output_tail:
        sections.append("Command output (tail):\n" + output_tail)
    if not sections:
        sections.append(
            "The test gate failed but the runner reported no failures, "
            "notices or command output. Check the test-runner logs."
        )
    return "No structured test failures were parsed.\n\n" + "\n\n".join(sections)


def _gate_exit_code(test_result: dict[str, Any]) -> int:
    """The gate result's exit code, negative when signal-terminated."""
    exit_code = test_result.get("exit_code")
    if isinstance(exit_code, int):
        return exit_code
    reported = _reported_exit_code(test_result)
    return reported if reported is not None else 0


def _render_seconds(value: Any) -> str:
    """Render a measured duration, naming an unmeasured one."""
    if isinstance(value, (int, float)):
        return f"{float(value):.1f}s"
    return "unknown"


def _render_gate_facts(facts: dict[str, Any]) -> str:
    """One line per known infrastructure fact, in a fixed readable order.

    The units are stated so a report is readable without the source: seconds
    for the durations, the command as it was run, and the exit code with the
    signal that terminated it when the child was killed
    (SA-0MVTET8KQ0031R6Z).
    """
    lines: list[str] = []
    if "configured_timeout" in facts:
        lines.append(
            "configured timeout (timeoutPerCommand): "
            f"{facts['configured_timeout']}s"
        )
    if "elapsed_seconds" in facts:
        lines.append(
            "suite elapsed (command execution only): "
            f"{_render_seconds(facts['elapsed_seconds'])}"
        )
    if "slot_wait_seconds" in facts:
        lines.append(
            "test-slot queue wait (separate from elapsed): "
            f"{_render_seconds(facts['slot_wait_seconds'])}"
        )
    if "command" in facts:
        lines.append(f"command: {facts['command']}")
    if "exit_code" in facts:
        lines.append(f"exit code: {facts['exit_code']}")
    if "signal" in facts:
        lines.append(f"signal: {facts['signal']} (child killed)")
    return "\n".join(lines)


def _gate_failure_message(
    test_result: dict[str, Any],
    attempt: int,
    max_retry: int,
    work_item_id: str,
) -> str:
    """Build the operator-facing message for a failed changed-scope run.

    Distinguishes "no free test slot" (a retryable infrastructure condition,
    with the longer-``TEST_LOCK_TIMEOUT`` remedy) from genuine test failures
    (WL-0MVGUT787000LNDG) and always includes the root-cause detail.
    """
    kind = _gate_failure_kind(test_result)
    detail = _gate_failure_detail(test_result)
    facts = _gate_facts(test_result, _gate_exit_code(test_result))
    state_changed_flags = test_result.get("state_changed_during_run") or []
    if isinstance(state_changed_flags, dict):
        state_changed_flags = [state_changed_flags]
    if state_changed_flags:
        headline = (
            f"Test run {attempt}/{max_retry} was invalidated: the checkout "
            "moved while the suite executed (infrastructure, not a code "
            "failure). Nothing ran against one single tree, so the verdict "
            "is discarded; re-running below executes from a stable checkout."
        )
    elif kind == GATE_FAILURE_SLOT_CONTENTION:
        waited = (
            f" after waiting {_render_seconds(facts['slot_wait_seconds'])} "
            "for one"
            if "slot_wait_seconds" in facts
            else ""
        )
        headline = (
            f"Test run {attempt}/{max_retry} could not start: no free test "
            f"slot{waited} (infrastructure condition, not a code failure). "
            "The queue wait is reported separately from the suite's "
            "execution time. Wait a staggered delay before retrying (do not "
            "re-run immediately; concurrent waiters re-enter the pool in "
            "lockstep otherwise), or raise TEST_LOCK_TIMEOUT "
            "(e.g. TEST_LOCK_TIMEOUT=1800) to wait longer."
        )
    elif kind == GATE_FAILURE_TIMEOUT:
        configured = facts.get("configured_timeout")
        budget = (
            f" The gate allows {configured}s per command (timeoutPerCommand)."
            if configured is not None
            else ""
        )
        headline = (
            f"Test run {attempt}/{max_retry} timed out: the suite exceeded "
            f"the configured per-command timeout and was stopped after "
            f"{_render_seconds(facts.get('elapsed_seconds'))} of execution "
            f"(infrastructure condition, not a code failure).{budget} Raise "
            "timeoutPerCommand in .pi/test-config.json with headroom over a "
            "green run's elapsed time (a queue wait for a test slot is "
            "reported separately and is not part of that budget), then "
            "re-run."
        )
    elif kind == GATE_FAILURE_KILLED:
        signal_number = facts.get("signal")
        headline = (
            f"Test run {attempt}/{max_retry} was killed"
            + (f" by signal {signal_number}" if signal_number is not None else "")
            + " (infrastructure condition, not a code failure): the child was "
            "terminated without reporting a test result — an OOM kill or a "
            "worker/supervisor crash. Inspect the output tail below; do not "
            "treat this as a failing test."
        )
    elif kind == GATE_FAILURE_UNPARSED:
        exit_code = facts.get("exit_code")
        headline = (
            f"Test run {attempt}/{max_retry} exited "
            + (f"with code {exit_code} " if exit_code is not None else "non-zero ")
            + "and produced no parsable test failures (infrastructure "
            "condition, not a code failure): the suite command failed before "
            "any test result was reported. Read the command and output tail "
            "below — the command or its configuration is the problem, not a "
            "test."
        )
    elif kind == GATE_FAILURE_INFRASTRUCTURE:
        headline = (
            f"Test run {attempt}/{max_retry} failed for an infrastructure "
            "reason (not a code failure)."
        )
    elif kind == GATE_FAILURE_UNKNOWN:
        headline = (
            f"Test run {attempt}/{max_retry} failed for an unclassified "
            "reason: the runner reported no structured failure, no notice and "
            "no executed command, so no view of this run can be attributed. "
            "Re-run the suite directly with the test skill to obtain "
            "diagnostics."
        )
    else:
        headline = f"Test run {attempt}/{max_retry} failed."
    return (
        f"{headline}\n\n{detail}\n\n"
        f"Please fix the failures and re-run:\n"
        f"  python3 scripts/implement.py finish {work_item_id}"
    )


def _run_guarded_suite(
    commands: list[str],
    cwd: str,
    scope: str,
    base_ref: str | None,
    tooling: str | None,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Execute *commands* through the canonical guard-owning runner.

    Delegates to ``run_tests.guarded_run_all`` (the single source of truth
    for live-repo guard ownership, SA-0MUH1MJL6003767Q) so implement.py's
    finish gate is guarded by the same outer snapshot guard as the test
    skill's CLI — even when ``LIVE_REPO_GUARD_ACTIVE`` is already set in the
    environment (a nested run): ``guarded_run_all`` then stands down because
    the outer guard already owns the checkout.

    *fingerprint* is the finish gate's would-be-commit tree SHA; it is
    forwarded to the cache so the verdict is keyed by the tree the commit
    produces rather than the loose working tree (SA-0MVQHVHXI007PQAB).

    When the test skill is unavailable (partial install), or the delegation
    raises, falls back to the direct ``_run_commands_direct`` path (the
    inner conftest guard remains armed), preserving graceful degradation.
    """
    if _guarded_run_all is None or _full_suite_commands is None:
        # Partial install, or the legacy convention path: only the inner
        # conftest guard is armed, so run directly (no delegation).
        with _scoped_test_tmpdir_export():
            return _run_commands_direct(
                commands,
                cwd,
                tooling=tooling,
                scope=scope,
                fingerprint=fingerprint,
            )
    try:
        # The in-process runner has no env to pass, so the resolved
        # disk-backed TMPDIR is exported into ``os.environ`` for the duration
        # of the call (save/restore — never mutates the parent shell). The
        # suite subprocesses the runner spawns also carry ``TMPDIR`` from
        # ``test.scripts.run_tests._suite_command_env``; the export here
        # additionally covers the runner's own tempfile usage and the direct
        # fallback path (SA-0MW3ENQY0001RBSW AC2/AC3).
        with _scoped_test_tmpdir_export():
            guarded = _guarded_run_all(
                cwd=Path(cwd),
                timeout=_resolve_test_timeout(cwd),
                commands=commands,
                scope=scope,
                base_ref=base_ref or "origin/dev",
                use_cache=True,
                fingerprint=fingerprint,
            )
    except Exception as exc:  # noqa: BLE001 - degrade to the direct path
        LOG.warning(
            "Implement run_tests: guarded_run_all failed (%s) — falling "
            "back to the direct cached runner (inner guard only).",
            exc,
        )
        with _scoped_test_tmpdir_export():
            return _run_commands_direct(
                commands, cwd, tooling=tooling, scope=scope, fingerprint=fingerprint
            )
    return _adapt_guarded_result(guarded, tooling=tooling, scope=scope)


def _run_changed_scope_pytest(
    cwd: str, base_ref: str | None = None, fingerprint: str | None = None
) -> dict[str, Any] | None:
    """Run only the tests affected by the worktree's changes vs *base_ref*.

    Reuses the shared changed-file → test selector from the test skill
    (``skill/test/scripts/run_tests.py``) so implement's scoped validation
    selects the exact same subset as ``run_tests.py --scope changed`` and
    caches it under a distinct key with ``scope: "changed"`` metadata (a
    partial run is never mistaken for full-suite evidence).

    Returns ``None`` when no subset is possible so the caller falls back to
    the full suite: no dev baseline/changed files, only non-test changes, a
    selection that resolves to non-pytest commands (this legacy channel can
    only run pytest), or a partial skill install without the test skill's
    selector. ``.pi/test-config.json`` ``suiteCommands`` does NOT disable
    selection — it describes the full suite only (SA-0MVL7YOMJ006UMDO).
    npm / repo-script / override / unity toolings are not subsettable — the
    caller warns and runs full scope. The resolved-suite path uses
    :func:`_run_changed_scope_selection` instead, which runs any tooling.
    """
    if _changed_scope_commands is None:
        LOG.warning(
            "Implement run_tests: changed-scope selector unavailable "
            "(test/scripts.run_tests not importable) — running full scope "
            "on %s.",
            cwd,
        )
        return None
    commands = _changed_scope_commands(Path(cwd), base_ref=base_ref or "origin/dev")
    if not commands:
        return None
    pytest_cmds = [c for c in commands if c.startswith("pytest")]
    if not pytest_cmds:
        return None
    run = _run_guarded_suite(
        [pytest_cmds[0]],
        cwd,
        scope="changed",
        base_ref=base_ref,
        tooling="pytest",
        fingerprint=fingerprint,
    )
    # pytest exit 4 is a *usage* error: "file or directory not found" (or a
    # similar no-valid-items condition). It means the selection referenced a
    # path that is not runnable — typically a deleted/renamed test file that
    # still appeared in `git diff`. That is not a test failure: return None so
    # the caller falls back to the full suite (fail-closed) instead of
    # aborting finish with a phantom "Test run failed". A genuine failure is
    # exit 1 and must still block (LP-0MTZYRTNF0092JKW).
    if run.get("exit_code") == 4:
        LOG.warning(
            "Implement run_tests: changed-scope pytest exited 4 (no valid "
            "test paths — likely a deleted/renamed test file) in %s — "
            "falling back to full scope.",
            cwd,
        )
        return None
    return run


def _changed_scope_unavailable_reason(cwd: str, base_ref: str | None = None) -> str:
    """The shared selector's specific reason for having no subset, or a
    generic fallback when the selector itself is unavailable.

    Keeping the reason in one place means every implement.py entry point
    (the resolved-suite path and the legacy pytest-only path) reports the
    same specific cause instead of restating a generic list of possibilities
    (SA-0MVL7YOMJ006UMDO).
    """
    if _changed_scope_selection is None:
        return "changed-scope selector unavailable"
    return _changed_scope_selection(
        Path(cwd), base_ref=base_ref or "origin/dev"
    ).reason


def _run_changed_scope_selection(
    cwd: str, base_ref: str | None = None, fingerprint: str | None = None
) -> tuple[dict[str, Any] | None, str]:
    """Run the changed-file-selected commands (any tooling) via the guard.

    Unlike :func:`_run_changed_scope_pytest` (the legacy pytest-only path),
    this runs whatever the shared selector resolves — a named group's
    commands, a vitest ``*.test.ts`` selection, pytest, or node — so a repo
    that declares ``suiteCommands`` or ``groups`` reports ``scope: changed``
    instead of falling back to the full suite (SA-0MVL7YOMJ006UMDO).

    *fingerprint* is the finish gate's would-be-commit tree SHA, forwarded to
    the run cache so the verdict is keyed by the tree the commit produces
    (SA-0MVQHVHXI007PQAB).

    Returns ``(result, reason)``. ``result`` is ``None`` when no subset is
    available; ``reason`` then names the specific fallback cause so the
    caller can log it instead of a generic list of possibilities.
    """
    if _changed_scope_commands is None:
        return None, "changed-scope selector unavailable"
    commands = _changed_scope_commands(
        Path(cwd), base_ref=base_ref or "origin/dev"
    )
    if not commands:
        return None, _changed_scope_unavailable_reason(cwd, base_ref=base_ref)
    run = _run_guarded_suite(
        list(commands),
        cwd,
        scope="changed",
        base_ref=base_ref,
        tooling="changed-scope",
        fingerprint=fingerprint,
    )
    # pytest exit 4 is a usage error ("file or directory not found"), not a
    # test failure. Treat it as selection unavailable so the caller falls
    # back to the full suite (LP-0MTZYRTNF0092JKW).
    if run.get("exit_code") == 4:
        LOG.warning(
            "Implement run_tests: changed-scope run exited 4 (no valid test "
            "paths — likely a deleted/renamed test file) in %s — falling "
            "back to full scope.",
            cwd,
        )
        return None, "selection references no valid test paths (exit 4)"
    return run, "changed-scope selection available"


def _run_commands(
    commands: list[str],
    cwd: str,
    tooling: str,
    scope: str = "full",
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Execute *commands* through the canonical guard-owning runner.

    Delegates to :func:`_run_guarded_suite` (which routes through the test
    skill's ``guarded_run_all``) so the canonical outer live-repo guard owns
    the checkout for every finish-gate suite (SA-0MUH1MJL6003767Q). Falls
    back to :func:`_run_commands_direct` when the test skill is unavailable.
    """
    if _guarded_run_all is not None and _full_suite_commands is not None:
        return _run_guarded_suite(
            commands,
            cwd,
            scope=scope,
            base_ref=None,
            tooling=tooling,
            fingerprint=fingerprint,
        )
    return _run_commands_direct(
        commands, cwd, tooling=tooling, scope=scope, fingerprint=fingerprint
    )


def _run_commands_direct(
    commands: list[str],
    cwd: str,
    tooling: str,
    scope: str = "full",
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Run each command through the per-repo cache and combine the results.

    Commands are resolved by the test skill's ``full_suite_commands`` /
    ``changed_scope_commands`` (the single source of truth) and executed in
    order through the paced cache runner. The first non-zero exit code is
    reported, and stdout/stderr are concatenated so a multi-command suite
    (e.g. pytest plus one command per node suite dir, or the declared
    ``suiteCommands``) yields one aggregated result.

    This is the direct (non-delegating) path used only when the test skill's
    guard-owning ``guarded_run_all`` is unavailable — a partial skill install
    where only the inner conftest guard is armed.

    Args:
        commands: Shell command strings to run in order.
        cwd: Worktree root.
        tooling: Tooling label recorded on the result.
        scope: ``full`` or ``changed`` (recorded as cache/evidence metadata).

    Returns:
        A finalised test result dict.
    """
    exit_code = 0
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    cached_flags: list[bool] = []
    cache_entries: list[dict[str, Any]] = []
    for command in commands:
        run = _run_cached_paced(
            command,
            cwd=cwd,
            timeout=_resolve_test_timeout(cwd),
            runner=lambda command, cwd_, timeout_: run_cmd(
                shlex.split(command), cwd=cwd_, check=False, timeout=timeout_, capture=True,
                keep_git_index=True,
            ),
            scope=scope,
            fingerprint=fingerprint,
        )
        cached_flags.append(bool(run.get("cached")))
        if run.get("cached"):
            cache_entries.append(_cache_entry_from_run(run, command))
        if run["exit_code"] != 0 and exit_code == 0:
            exit_code = run["exit_code"]
        if run.get("stdout"):
            stdout_parts.append(run["stdout"])
        if run.get("stderr"):
            stderr_parts.append(run["stderr"])
    return _with_cache_provenance(
        _finalize_test_result(
            {
                "exit_code": exit_code,
                "stdout": "\n".join(stdout_parts),
                "stderr": "\n".join(stderr_parts),
            },
            tooling=tooling,
            scope=scope,
        ),
        cached=bool(cached_flags) and all(cached_flags),
        entries=cache_entries,
    )


def _run_group_from_changed_files(
    cwd: str, base_ref: str | None = None, fingerprint: str | None = None
) -> dict[str, Any] | None:
    """Try to select a named test group from changed files and run it.

    Uses the shared :func:`select_group_from_changed_files` to map touched
    paths to the smallest sufficient group (SA-0MUJKF2HP001BR51).  When a
    group is selected, its commands are executed through the cache.  Returns
    None when no groups are defined or no group matches — the caller falls
    back to the full suite.

    This runs BEFORE the existing changed-file → test-file selector because
    a group is a coarser but still targeted scope: an audit-skill change
    triggers the ``audit`` group (not every test in the repo).
    """
    try:
        import run_tests as _rt
        from run_tests import (
            group_commands as _group_cmds,
        )
        from run_tests import (
            select_group_from_changed_files as _select_group,
        )
    except ImportError:
        return None

    changed = _rt.compute_changed_files(Path(cwd), base_ref=base_ref or "origin/dev")
    if not changed:
        return None

    group_name = _select_group(changed, Path(cwd))
    if group_name is None:
        return None

    group_cmds = _group_cmds(group_name, Path(cwd))
    if group_cmds is None:
        return None

    return _run_commands(
        group_cmds,
        cwd,
        tooling=f"group:{group_name}",
        scope="changed",
        fingerprint=fingerprint,
    )


def _resolve_full_suite_commands(cwd: str) -> list[str] | None:
    """Resolve the repo's full suite via the test skill's single source of truth.

    Delegates to ``full_suite_commands`` (extension ``suiteCommands`` first,
    then convention detection: pytest → node suites → npm-test). Returns
    ``None`` when the test skill is unavailable (partial install) or the
    repo has no suite at all, so the caller can fall back to implement.py's
    legacy convention detection (which additionally supports repo-local
    runner scripts and Unity). Returns ``[]`` only when the repo explicitly
    declares an empty ``suiteCommands`` list — authoritative "no runnable
    suite", never a convention fallback (F2 AC1).
    """
    if _full_suite_commands is None:
        return None
    root = Path(cwd).resolve()
    try:
        commands = list(_full_suite_commands(root))
    except Exception as exc:  # noqa: BLE001 - degrade to convention detection
        LOG.warning(
            "Implement run_tests: full_suite_commands(%s) failed (%s) — "
            "falling back to convention detection.",
            root,
            exc,
        )
        return None
    if commands:
        return commands
    # Empty result: authoritative only when the extension file explicitly
    # declares suiteCommands (including an empty list).
    if _extension_suite_commands is not None:
        try:
            declared = _extension_suite_commands(root)
        except Exception:  # noqa: BLE001 - treat as undeclared
            declared = None
        if declared is not None:
            return []
    return None


def _run_resolved_suite(
    cwd: str,
    commands: list[str],
    scope: str = "changed",
    base_ref: str | None = None,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Run a resolved full-suite command list, honouring changed scope.

    ``scope="changed"`` first tries the test skill's changed-file selector
    (named group, then file-level selection). ``.pi/test-config.json``
    ``suiteCommands`` never disables selection — it describes the full suite
    only (SA-0MVL7YOMJ006UMDO). When no subset is available the selector's
    specific reason is warned and the full command list runs, so a scoped run
    never silently skips testing.
    """
    if scope == "changed":
        group_result = _run_group_from_changed_files(
            cwd, base_ref=base_ref, fingerprint=fingerprint
        )
        if group_result is not None:
            LOG.info(
                "Implement run_tests: selected group '%s' from changed files in %s",
                group_result.get("tooling", "unknown"),
                cwd,
            )
            return group_result
        scoped, scope_reason = _run_changed_scope_selection(
            cwd, base_ref=base_ref, fingerprint=fingerprint
        )
        if scoped is not None:
            return scoped
        LOG.warning(
            "Implement run_tests: changed-scope selection unavailable in "
            "%s (%s) — falling back to full scope.",
            cwd,
            scope_reason,
        )
    return _run_commands(
        commands, cwd, tooling="suite", scope="full", fingerprint=fingerprint
    )


def run_tests(cwd: str, scope: str = "changed",
              base_ref: str | None = None,
              fingerprint: str | None = None) -> dict[str, Any]:
    """Run the project test suite, routed through the per-repo run cache.

    *scope* selects the execution scope (SA-0MT6CENPW004V2JN):

    - ``changed`` (default): run only the tests affected by the worktree's
      changes vs *base_ref* (``origin/dev`` default). Used by the finish
      build/test/commit loop for fast iteration. Selection order:

      1. Named group from ``.pi/test-config.json`` → mapped from changed
         paths (SA-0MUJKF2HP001BR51).
      2. Changed-file → test-file selector (existing behaviour).
      3. Full suite (fallback when nothing is subsettable).

      Falls back to the full suite with a logged warning when no subset can
      be selected — a scoped run must never silently skip testing.
    - ``full``: the complete suite (populates/consumes the full-suite cache
      entry that audit and the pre-push hook rely on).

    The test step is tolerant of repos with no test tooling: when neither
    pytest, an npm ``test`` script, nor a repo-local runner is detected, the
    step is skipped and reported as a no-op (``success: True``,
    ``skipped: True``) so finish proceeds to commit → push instead of
    aborting on ENOENT / ``Missing script: "test"``.

    Suite resolution order (single source of truth, F2 AC4):

    1. ``IMPLEMENT_TEST_COMMAND`` env var — per-repo override; run as-is.
    2. The test skill's ``full_suite_commands`` — ``.pi/test-config.json``
       ``suiteCommands`` first, then convention detection (pytest → node
       suite dirs → npm-test). When ``suiteCommands`` is declared it is the
       primary list and convention detection is skipped (F2 AC1).
    3. Legacy convention detection (only when the test skill is unavailable
       or resolved no suite): pytest → npm → repo-local runner → Unity →
       skip. This preserves implement.py's superset support for repos the
       test skill does not model (``run_tests.sh``, Unity projects).

    Repos WITH tooling behave as before: the resolved command(s) run through
    ``skill.test_cache.run_cached`` — a valid cached result for the same
    worktree git state within the TTL is reused (SA-0MSGN5OJ4002OZKY) — and
    a non-zero exit still blocks finish. Multiple resolved commands (e.g. a
    declared ``suiteCommands`` list, or pytest plus node suite dirs) all run
    and their results are combined.

    Guard ownership (SA-0MUH1MJL6003767Q): when the test skill is available,
    execution is delegated to the canonical ``run_tests.guarded_run_all``
    (the single source of truth for the outer live-repo guard) instead of
    arming a second guard here. That keeps the finish gate guarded by the
    same snapshot guard as the test skill's CLI, even when
    ``LIVE_REPO_GUARD_ACTIVE`` is already set in the environment (a nested
    run): ``guarded_run_all`` then stands down because an outer guard already
    owns the checkout. When the test skill is unavailable (a partial install),
    the direct cached path runs and the inner conftest guard remains armed.

    Args:
        cwd: Working directory (worktree root).
        scope: Execution scope (``changed`` — default — or ``full``).
        base_ref: Base ref for changed-file detection when *scope* is
            ``changed`` (default ``origin/dev``).
        fingerprint: Explicit expected fingerprint for the run cache — the
            finish gate passes the would-be-commit tree SHA
            (``git write-tree`` of its temporary index). When supplied, the
            cache key and verdict acceptance are bound to that tree instead of
            the loose working-tree state, so a verdict recorded for another
            tree (or a legacy entry without provenance) misses and the suite
            re-runs (SA-0MVQHVHXI007PQAB). Omitted → historical behaviour.

    Returns:
        A dict with ``success`` (bool), ``stdout`` (str), ``stderr`` (str),
        ``exit_code`` (int), ``failures`` (list[str]), ``scope`` (str), plus
        ``skipped`` (bool — True when no tooling was found) and ``tooling``
        (str | None — the resolved runner: ``suite`` (test-skill resolution),
        pytest/npm/repo-script/override/unity).
    """
    # 1. Per-repo override (env var) — highest precedence
    override = os.environ.get("IMPLEMENT_TEST_COMMAND", "").strip()
    if override:
        if scope == "changed":
            LOG.warning(
                "Implement run_tests: IMPLEMENT_TEST_COMMAND override is not "
                "subsettable — running full scope on %s.",
                cwd,
            )
        return _finalize_test_result(
            _run_cached_paced(
                override,
                cwd=cwd,
                timeout=_resolve_test_timeout(cwd),
                runner=_shell_command_runner,
                scope="full",
                fingerprint=fingerprint,
            ),
            tooling="override",
            scope="full",
        )

    # 2. Delegate to the test skill's single source of truth for suite
    #    resolution (F2 AC4 / WL-0MUL6LCE00042MB0): ``suiteCommands`` first,
    #    then convention detection (pytest → node suites → npm-test). This
    #    ensures the finish gate exercises the repo's real full suite (e.g.
    #    ContextHub's vitest commands) instead of implement.py's duplicate
    #    pytest-first detection. ``None`` means the skill is unavailable or
    #    no suite was resolved, so the legacy convention detection below
    #    still handles repo-local runners / Unity / no-tooling repos.
    resolved_suite = _resolve_full_suite_commands(cwd)
    if resolved_suite is not None:
        if not resolved_suite:
            return _skip_test_result(
                "No test suite declared or detected — skipping test step "
                "(no-op).",
                tooling=None,
            )
        return _run_resolved_suite(
            cwd,
            resolved_suite,
            scope=scope,
            base_ref=base_ref,
            fingerprint=fingerprint,
        )

    # 3. Legacy convention detection (test skill unavailable or no suite
    #    resolved): preserve implement.py's superset support for repo-local
    #    runner scripts and Unity projects.
    tooling = _detect_test_tooling(cwd)

    # 3a. pytest (with npm test fallback when the repo also has a test script)
    if tooling == "pytest":
        if scope == "changed":
            # Try named group selection first (SA-0MUJKF2HP001BR51):
            # map changed files to the smallest sufficient group.
            group_result = _run_group_from_changed_files(
                cwd, base_ref=base_ref, fingerprint=fingerprint
            )
            if group_result is not None:
                LOG.info(
                    "Implement run_tests: selected group '%s' from changed files in %s",
                    group_result.get("tooling", "unknown"),
                    cwd,
                )
                return group_result
            # Fall through to the existing changed-file → test-file selector.
            scoped = _run_changed_scope_pytest(
                cwd, base_ref=base_ref, fingerprint=fingerprint
            )
            if scoped is not None:
                return scoped
            LOG.warning(
                "Implement run_tests: changed-scope selection unavailable in "
                "%s (%s) — falling back to full scope.",
                cwd,
                _changed_scope_unavailable_reason(cwd, base_ref=base_ref),
            )
        pytest_run = _run_cached_paced(
            PYTEST_CMD,
            cwd=cwd,
            timeout=_resolve_test_timeout(cwd),
            runner=lambda command, cwd_, timeout_: run_cmd(
                shlex.split(command), cwd=cwd_, check=False, timeout=timeout_, capture=True,
                keep_git_index=True,
            ),
            scope="full",
            fingerprint=fingerprint,
        )
        result = pytest_run
        final_tooling = "pytest"
        if result["exit_code"] != 0 and _has_test_script(cwd):
            # Try npm test as fallback (also cached, canonical form)
            npm_run = _run_cached_paced(
                NPM_TEST_CMD,
                cwd=cwd,
                timeout=_resolve_test_timeout(cwd),
                runner=lambda command, cwd_, timeout_: run_cmd(
                    shlex.split(command), cwd=cwd_, check=False, timeout=timeout_, capture=True,
                    keep_git_index=True,
                ),
                scope="full",
                fingerprint=fingerprint,
            )
            if npm_run["exit_code"] == 0:
                return _finalize_test_result(
                    npm_run, tooling="npm", scope="full"
                )
            # Use the npm result if pytest also failed
            result = npm_run
            final_tooling = "npm"
        return _finalize_test_result(result, tooling=final_tooling, scope="full")

    # 3b. npm test script (no pytest suite)
    if tooling == "npm":
        if scope == "changed":
            LOG.warning(
                "Implement run_tests: npm tooling is not subsettable — "
                "running full scope on %s.",
                cwd,
            )
        return _finalize_test_result(
            _run_cached_paced(
                NPM_TEST_CMD,
                cwd=cwd,
                timeout=_resolve_test_timeout(cwd),
                runner=lambda command, cwd_, timeout_: run_cmd(
                    shlex.split(command), cwd=cwd_, check=False, timeout=timeout_, capture=True,
                    keep_git_index=True,
                ),
                scope="full",
                fingerprint=fingerprint,
            ),
            tooling="npm",
            scope="full",
        )

    # 3c. Repo-local runner script
    if tooling == "repo-script":
        runner_script = _find_repo_test_runner(cwd)
        if runner_script is None:
            return _skip_test_result(
                "Repo-local runner script disappeared before execution — "
                "skipping test step (no-op).",
                tooling="repo-script",
            )
        if runner_script.endswith(".bat"):
            if shutil.which("cmd.exe") is None:
                return _skip_test_result(
                    f"Repo-local test runner {runner_script} requires cmd.exe "
                    "which is not available on this host — skipping test step "
                    "(no-op).",
                    tooling="repo-script",
                )
            command = f"cmd.exe /c {runner_script}"
        else:
            command = f"bash {runner_script}"
        if scope == "changed":
            LOG.warning(
                "Implement run_tests: repo-local runner is not subsettable — "
                "running full scope on %s.",
                cwd,
            )
        return _finalize_test_result(
            _run_cached_paced(
                command,
                cwd=cwd,
                timeout=_resolve_test_timeout(cwd),
                runner=_shell_command_runner,
                scope="full",
                fingerprint=fingerprint,
            ),
            tooling="repo-script",
            scope="full",
        )

    # 3d. Unity project without a configured runner → Unity-specific skip
    if tooling == "unity":
        return _skip_test_result(
            "Unity project detected (Assets/ or ProjectSettings/ProjectVersion.txt) "
            "but no test runner configured — no pytest suite, no npm test script, "
            "and no repo-local runner (run_tests.sh / run_unity_tests.sh / "
            "run_unity_tests.bat). Skipping test step (no-op). Set "
            "IMPLEMENT_TEST_COMMAND to run Unity tests if needed.",
            tooling="unity",
        )

    # 3e. No tooling at all → generic skip
    return _skip_test_result(
        "No test tooling detected (no pytest suite, no npm test script, no "
        "repo-local test runner) — skipping test step (no-op).",
        tooling=None,
    )


def run_refactor(work_item_id: str, cwd: str) -> dict[str, Any]:
    """Run the refactor step.

    Args:
        work_item_id: The work item ID (for context).
        cwd: Working directory (worktree root).

    Returns:
        A dict with ``success`` (bool) and ``report`` (dict).
    """
    # Resolve refactor script relative to skills root
    skills_root = Path(__file__).resolve().parents[2]  # .../.pi/agent/skills/
    refactor_path = skills_root / "refactor" / "scripts" / "refactor.py"

    if not refactor_path.exists():
        # Fallback: try the relative path defined in the old SKILL.md convention
        refactor_path = Path(cwd) / "skill" / "refactor" / "scripts" / "refactor.py"
    if not refactor_path.exists():
        LOG.warning("Refactor script not found at %s; skipping refactor step", refactor_path)
        return {"success": True, "report": {"skipped": True, "reason": "script_not_found"}}

    result = run_cmd(
        ["python3", str(refactor_path), work_item_id, "--json"],
        cwd=cwd,
        check=False,
        timeout=300,
        capture=True,
    )
    report: dict[str, Any] = {"skipped": False}
    if result.stdout.strip():
        try:
            report = json.loads(result.stdout.strip())
        except (json.JSONDecodeError, ValueError):
            report["raw_output"] = result.stdout.strip()

    return {
        "success": result.returncode == 0,
        "report": report,
    }


# ---------------------------------------------------------------------------
# State file management
# ---------------------------------------------------------------------------


def write_state(state: ImplementState, worktree_path: str) -> None:
    """Write the implement state file into the worktree.

    Args:
        state: The current implement state.
        worktree_path: Path to the worktree root.
    """
    state_path = Path(worktree_path) / STATE_FILE_NAME
    state_path.write_text(json.dumps(state.to_dict(), indent=2))
    LOG.debug("State written to %s", state_path)


def read_state(worktree_path: str) -> ImplementState | None:
    """Read the implement state file from the worktree.

    Args:
        worktree_path: Path to the worktree root.

    Returns:
        ``ImplementState`` if found, else ``None``.
    """
    state_path = Path(worktree_path) / STATE_FILE_NAME
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text())
        return ImplementState.from_dict(data)
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        LOG.warning("Failed to read state file %s: %s", state_path, exc)
        return None


def remove_state(worktree_path: str) -> None:
    """Remove the implement state file from the worktree.

    Args:
        worktree_path: Path to the worktree root.
    """
    state_path = Path(worktree_path) / STATE_FILE_NAME
    if state_path.exists():
        state_path.unlink()
        LOG.debug("State file removed: %s", state_path)


# ---------------------------------------------------------------------------
# Stash hygiene helpers
# ---------------------------------------------------------------------------


def git_stash_list(cwd: str | None = None) -> str:
    """Run ``git stash list`` and return output.

    Args:
        cwd: Working directory (defaults to current directory).

    Returns:
        Stash list output, or empty string on error.
    """
    result = run_cmd(
        ["git", "stash", "list"],
        cwd=cwd,
        capture=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _extract_work_item_ids_from_stash(stash_entry: str) -> list[str]:
    """Extract work-item IDs from a single stash line.

    Stash lines have the format:
    ``stash@{N}: On <branch>: <message>``

    Work-item IDs in the message follow the pattern ``SA-XXXXXXXXXXX`` or
    any prefix followed by ``-`` and alphanumeric characters.

    Args:
        stash_entry: A single line from ``git stash list``.

    Returns:
        List of work-item ID strings found in the stash message.
    """
    # Pattern: SA-<word characters>, or any-prefix-<word characters>
    # The stash message portion comes after "On <branch>: "
    match = re.search(r"On\s+\S+:\s*(.*)", stash_entry)
    if not match:
        return []
    message = match.group(1)
    # Match work-item IDs: prefix (like SA, WL, etc.) followed by - and word chars
    return re.findall(r"\b([A-Z]+-[A-Za-z0-9]+)\b", message)


def _is_work_item_open(work_item_id: str) -> bool:
    """Check whether a work item is currently open (not terminal).

    Uses ``wl show`` to fetch the item and checks if its status is
    ``in-progress``, ``in_progress``, ``open``, or ``blocked``.

    Args:
        work_item_id: The work item ID to check.

    Returns:
        True if the work item is open (not in_review/completed/done/deleted).
    """
    try:
        cmd = ["wl", "show", work_item_id, "--json"]
        cmd[1:1] = resolve_worklog_flags(cmd)
        result = run_cmd(cmd, check=False, timeout=30)
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout.strip())
        if isinstance(data, dict) and "workItem" in data:
            status = data["workItem"].get("status", "")
            return status in ("open", "in-progress", "in_progress", "blocked")
    except (json.JSONDecodeError, ValueError, RuntimeError):
        pass
    return False


@dataclass
class OrphanedStash:
    """Represents a stash entry that is orphaned (no matching open work item)."""

    stash_name: str  # e.g. "stash@{0}"
    stash_entry: str  # Full git stash list line
    matched_ids: list[str]  # Work item IDs found in the stash message
    is_orphaned: bool  # True if no matched ID is open


def check_orphaned_stashes(
    cwd: str | None = None,
) -> dict[str, Any]:
    """Inspect ``git stash list`` and identify orphaned stashes.

    An orphaned stash is one whose stash message does not reference any
    work item that is currently in an open state (open, in_progress, blocked).
    Stashes that reference an open work item are considered "matched" and
    are NOT orphaned.

    This function is called from ``phase_start`` to issue a WARNING when
    orphaned stashes are found, but does NOT block (fail-open). Use
    ``implement.py start --allow-orphaned-stashes`` to acknowledge the
    warning and proceed.

    Args:
        cwd: Working directory to check (defaults to current directory).

    Returns:
        A dict with:
        - ``total_stashes``: int — total number of stashes
        - ``orphaned_stashes``: list[dict] — orphaned stash entries
        - ``matched_stashes``: list[dict] — stashes matched to open work items
        - ``has_orphaned``: bool — True if any orphaned stashes were found
        - ``warning``: str | None — warning message if orphaned stashes exist
    """
    stash_output = git_stash_list(cwd)
    if not stash_output:
        return {
            "total_stashes": 0,
            "orphaned_stashes": [],
            "matched_stashes": [],
            "has_orphaned": False,
            "warning": None,
        }

    all_stashes: list[OrphanedStash] = []
    for line in stash_output.splitlines():
        # Parse stash name from "stash@{N}: On <branch>: <message>"
        stash_match = re.match(r"^(stash@\{\d+\}):", line)
        if not stash_match:
            continue
        stash_name = stash_match.group(1)
        matched_ids = _extract_work_item_ids_from_stash(line)

        if matched_ids:
            # Check if any matched ID is open
            any_open = any(
                _is_work_item_open(wid) for wid in matched_ids
            )
            is_orphaned = not any_open
        else:
            # No work item IDs found — this is orphaned
            is_orphaned = True

        all_stashes.append(OrphanedStash(
            stash_name=stash_name,
            stash_entry=line,
            matched_ids=matched_ids,
            is_orphaned=is_orphaned,
        ))

    orphaned = [s for s in all_stashes if s.is_orphaned]
    matched = [s for s in all_stashes if not s.is_orphaned]

    result: dict[str, Any] = {
        "total_stashes": len(all_stashes),
        "orphaned_stashes": [
            {
                "stash_name": s.stash_name,
                "stash_entry": s.stash_entry,
                "matched_ids": s.matched_ids,
            }
            for s in orphaned
        ],
        "matched_stashes": [
            {
                "stash_name": s.stash_name,
                "stash_entry": s.stash_entry,
                "matched_ids": s.matched_ids,
            }
            for s in matched
        ],
        "has_orphaned": len(orphaned) > 0,
        "warning": None,
    }

    if orphaned:
        orphaned_lines = "\n".join(
            f"  - {s.stash_name}: {s.stash_entry}" for s in orphaned
        )
        result["warning"] = (
            f"WARNING: {len(orphaned)} orphaned stash(es) detected on the main checkout. "
            f"These stashes do not reference any open work item and may be lost.\n\n"
            f"Orphaned stashes:\n{orphaned_lines}\n\n"
            f"Triage these stashes (restore-and-commit via a proper work item, or delete if stale).\n"
            f"Proceed anyway with --allow-orphaned-stashes."
        )

    return result


# ---------------------------------------------------------------------------
# Phase implementations
# ---------------------------------------------------------------------------


def phase_start(
    work_item_id: str,
    json_output: bool = False,
    no_refactor: bool = False,
    parent_branch: str = DEFAULT_PARENT_BRANCH,
    worktree_path_override: str | None = None,
    max_retry: int = DEFAULT_MAX_RETRY,
    verbose: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Phase 1: Setup the implementation environment.

    Steps:
    1. Validate the work item ID
    2. Code Freeze gate: refuse when a release is in progress
    2.5. Acquire the per-work-item child lock (before the claim and before
        any worktree creation/resume); a held child is reported as
        ``skipped-held`` and performs no writes (AC1)
    2.6. Outbound (depends-on) gate: refuse when the item's own same-project
        outbound blockers are unmet — reported as a ``blocked`` verdict with
        the blocker list, before the claim so no claim/worktree is left
        behind (SA-0MVS1QS9J009DGPT)
    3. Claim the work item (status → in_progress)
    4. Safety gate: check for dirty working tree
    5. Stash hygiene gate: warn on orphaned stashes (fail-open, --allow-orphaned-stashes to skip)
    6. Fetch work item details (audit)
    6.5. Refresh parent branch from origin (AC3)
    7. Create a worktree from the parent branch
    8. Register signal handlers
    9. Write persistent state

    The lock is held for the whole of the phase and released on normal exit,
    on exception/abort and on SIGINT/SIGTERM (AC2). Two concurrent
    ``drive``/``parent``/``start`` invocations for one child can therefore
    never both enter its critical section (SA-0MVOABYW00004978).

    Args:
        work_item_id: The work item ID.
        json_output: If True, output JSON.
        no_refactor: If True, skip refactor step.
        parent_branch: Parent branch for worktree.
        worktree_path_override: Override worktree path.
        max_retry: Max test-fix retries.
        verbose: Enable verbose logging.
        force: Acknowledge orphaned-stash warnings (--allow-orphaned-stashes) and proceed.

    Returns:
        Dict with result information.
    """
    report: dict[str, Any] = {
        "phase": "start",
        "work_item_id": work_item_id,
        "success": True,
        "worktree_path": "",
        "branch": "",
        "message": "",
    }

    # ── Step 1: Validate work item ID ──────────────────────────────
    if not WORK_ITEM_ID_PATTERN.match(work_item_id):
        msg = f"Invalid work item ID format: {work_item_id}. Expected pattern like SA-XXXXXXXXXXX"
        report["success"] = False
        report["message"] = msg
        if json_output:
            print(format_json_output(report))
        else:
            LOG.error(msg)
        return report

    # ── Step 2: Code Freeze gate (before claiming) ────────────────
    if is_code_freeze_active():
        msg = (
            "Project is in Code Freeze — implementation blocked until the "
            "release completes."
        )
        LOG.error(msg)
        report["success"] = False
        report["message"] = msg
        report["code_freeze"] = True
        if json_output:
            print(format_json_output(report))
        else:
            print(f"\n⛔ {msg}\n")
        return report

    # ── Step 2.5: Acquire the per-child lock (AC1) ────────────────
    # The single choke point for ``drive``, ``parent`` and a manual ``start``.
    # Acquired BEFORE the claim and before any worktree creation/resume so a
    # contender can never mutate the item or its worktree. A failed acquire
    # is fail-closed: report the holder and write nothing.
    try:
        lock = _new_child_lock(work_item_id)
        lock_result = lock.acquire()
    except (OSError, RuntimeError) as exc:
        msg = (
            f"Cannot acquire the per-child lock for {work_item_id}: {exc}. "
            "Refusing to start (fail closed) because exclusive ownership "
            "cannot be guaranteed."
        )
        LOG.error(msg)
        report["success"] = False
        report["message"] = msg
        if json_output:
            print(format_json_output(report))
        return report

    if not lock_result.acquired:
        _skipped_held_report(
            report, work_item_id, lock_result.held_by, lock_result.path,
        )
        lock.release()  # a losing handle never held the kernel lock: no-op
        LOG.warning("%s", report["message"])
        if json_output:
            print(format_json_output(report))
        else:
            print(f"\n⛔ {report['message']}\n")
        return report

    _set_child_lock(lock)
    if lock_result.reentrant:
        LOG.info(
            "Re-entered the per-child lock for %s by ownership token "
            "(holder chain); not blocked.",
            work_item_id,
        )
    elif lock_result.reclaimed:
        # Logged AND surfaced in the report (parent AC3, SA-0MVUX6Q4S006KII3).
        LOG.warning(
            "%s", _record_child_lock_reclamation(report, work_item_id, lock_result)
        )

    # Register the signal handler from the claim onwards: an interruption
    # between the claim and the worktree creation must release the lock and
    # leave an explanatory comment (AC2).
    _register_signal_handlers()
    try:
        return _phase_start_locked(
            report,
            work_item_id,
            json_output=json_output,
            parent_branch=parent_branch,
            worktree_path_override=worktree_path_override,
            force=force,
        )
    except Exception as exc:  # noqa: BLE001 - release the claim, then report
        # The claim is written before this point, so an *unexpected* failure
        # (anything the classified abort branches above did not catch) must
        # not leave the item claimed on a scheduling hold. The conditional
        # safety reset releases the assignee and returns an in-progress item
        # to open; it is a no-op when the item already reached a terminal
        # state, so a late error can never clobber one
        # (SA-0MVVF75ZA007XNKB).
        report["unexpected_error"] = f"{type(exc).__name__}: {exc}"
        report["success"] = False
        report["message"] = f"Unexpected error during start: {exc}"
        LOG.error("Unexpected error during start of %s: %s", work_item_id, exc)
        try:
            _safety_reset_if_in_progress(work_item_id)
        except RuntimeError:
            LOG.error(
                "Failed to release the claim on %s after an unexpected "
                "error",
                work_item_id,
            )
        if json_output:
            print(format_json_output(report))
        return report
    finally:
        # Normal exit, early return and exception all release the lock; the
        # signal handler releases it explicitly before ``os._exit``.
        _release_child_lock()


def _phase_start_locked(
    report: dict[str, Any],
    work_item_id: str,
    *,
    json_output: bool,
    parent_branch: str,
    worktree_path_override: str | None,
    force: bool,
) -> dict[str, Any]:
    """Steps 3-9 of :func:`phase_start`, executed under the child lock.

    Split out purely so the lock's acquisition/release brackets the whole
    mutating section without re-indenting it. ``report`` is populated by the
    caller (validation and Code Freeze gates) and returned here.
    """

    # ── Step 3.0: Outbound (depends-on) gate (SA-0MVS1QS9J009DGPT) ──
    # The invoked item's own outbound edges are authoritative producer
    # sequencing decisions: an unmet same-project blocker means the item must
    # wait. The gate sits BEFORE the claim (Assumption A3) so a blocked item
    # is never left ``in_progress`` with no worktree, and no worktree is
    # created. It runs *under* the per-child lock: a losing contender must
    # leave the worklog store untouched (no read/write at all), so the gate
    # cannot run before the lock is acquired. Cross-project edges remain
    # coordination notes and do not refuse (SA-0MSQBM2FK005NW1T).
    outbound = _outbound_gate(work_item_id)
    if outbound["coordination_blockers"]:
        LOG.info(
            "Cross-project dependency note(s) for %s: %s",
            work_item_id,
            _format_blockers(outbound["coordination_blockers"]),
        )
        report["coordination_blockers"] = outbound["coordination_blockers"]
    if outbound["blocked"]:
        msg = (
            f"Work item {work_item_id} is blocked by unmet outbound "
            f"dependency: {_format_blockers(outbound['blockers'])}. "
            "No implementation was started (no claim, no worktree). Wait for "
            "the blocker(s) to reach a terminal state, or remove the "
            "dependency edge, then re-run."
        )
        LOG.warning(msg)
        report["success"] = False
        report["blocked"] = True
        report["blocked_by"] = outbound["blockers"]
        report["message"] = msg
        if json_output:
            print(format_json_output(report))
        else:
            print(f"\n\u26d4 {msg}\n")
        return report

    # ── Step 3: Claim the work item via shared helper ─────────────
    # Status-only claim: the workflow stage must NOT be advanced here.
    # `in_progress` is a status, not a stage; passing it as a stage made `wl`
    # reject the whole atomic update and (previously) left the item unclaimed
    # while the failure was swallowed as a warning (SA-0MUY9PMB7001ENMP).
    LOG.info("Claiming work item %s...", work_item_id)
    try:
        StatusLifecycle.update_status(work_item_id, "in_progress")
        # Status-only claim: no assignee is set, so there is no scheduling hold
        # to release while the run is in flight (AGENTS_GLOBAL.md claim
        # pattern). Terminal outcomes below still write ``assignee=""``
        # explicitly, including on paths that never reach a StatusLifecycle
        # exit (SA-0MVVF6Y38004T5OZ).
    except RuntimeError as exc:
        msg = f"Failed to claim work item {work_item_id}: {exc}"
        report["success"] = False
        report["message"] = msg
        # Surface a genuine claim failure loudly, regardless of output mode.
        LOG.error(msg)
        if json_output:
            print(format_json_output(report))
        return report

    # ── Step 4: Load project-local extension ───────────────────────
    # The extension supplies both the prose hooks and the project-scoped
    # expected-dirty declaration honoured by the deferred dirty-tree safety
    # gate below (SA-0MUYEV5AO006V0HK, SA-0MV0NSOGE000OSMR). Absence is a
    # no-op; malformed data fails loudly rather than silently changing the
    # gate.
    repo_root = _get_repo_root() or str(Path.cwd().resolve())
    try:
        extension = load_implement_extension(repo_root)
        expected_dirty = _parse_expected_dirty(extension)
    except SkillExtensionError as exc:
        msg = f"Invalid implement skill extension: {exc}"
        LOG.error(msg)
        report["success"] = False
        report["message"] = msg
        report["extension_error"] = True
        try:
            StatusLifecycle.update_status(work_item_id, "open", assignee="")
        except RuntimeError:
            LOG.error("Failed to reset work item %s status to open", work_item_id)
        if json_output:
            print(format_json_output(report))
        return report
    report["extension"] = {
        "present": extension is not None,
        "expected_dirty_paths": list(expected_dirty),
    }

    # ── Step 5: Stash hygiene gate (warn on orphaned stashes) ─────
    LOG.info("Checking for orphaned stashes...")
    stash_result = check_orphaned_stashes()
    if stash_result["has_orphaned"]:
        if not force:
            orphaned_msg = (
                f"WARNING: {len(stash_result['orphaned_stashes'])} orphaned stash(es) detected on the main checkout. "
                f"These stashes do not reference any open work item and may be lost.\n"
            )
            if stash_result["warning"]:
                orphaned_msg += stash_result["warning"]
            LOG.warning("Orphaned stashes found:\n%s", orphaned_msg)
            if not json_output:
                print("\n⚠  Orphaned stash(es) detected")
                print("=" * 60)
                print(orphaned_msg)
                print("=" * 60)
                print("Run with --allow-orphaned-stashes to acknowledge and proceed.\n")
            # Record the warning in the work-item comment for audit trail
            wl_add_comment(work_item_id, f"Start phase: {len(stash_result['orphaned_stashes'])} orphaned stash(es) detected (see above). Run with --allow-orphaned-stashes to acknowledge.")
            # Don't block — just warn. Proceed regardless.
        else:
            LOG.info("--allow-orphaned-stashes: acknowledged orphaned stash warning.")
            if not json_output:
                print(f"\n⚠  {len(stash_result['orphaned_stashes'])} orphaned stash(es) detected (--allow-orphaned-stashes: proceeding)\n")
    elif stash_result["total_stashes"] > 0:
        LOG.info("All %d stash(es) matched to open work items — no orphaned stashes.", stash_result["total_stashes"])
    else:
        LOG.info("No stashes found.")

    # Surface the project-local prose hook before the first actionable step
    # (worktree implementation), after all gating steps have passed.
    _surface_extension_prose(extension, "prefix", json_output, report)

    # ── Step 6: Fetch work item details ────────────────────────────
    LOG.info("Fetching work item %s...", work_item_id)
    work_item = wl_show(work_item_id)
    if not work_item:
        msg = f"Work item {work_item_id} not found or failed to fetch"
        report["success"] = False
        report["message"] = msg
        try:
            StatusLifecycle.update_status(work_item_id, "open", assignee="")
        except RuntimeError:
            LOG.error("Failed to reset work item %s status to open", work_item_id)
        if json_output:
            print(format_json_output(report))
        else:
            LOG.error(msg)
        return report

    title = work_item.get("title", work_item_id)
    slug = slug_from_title(title)

    # ── Step 6.05: Safety gate (dirty working tree, Key-Files-aware) ──
    # Deferred until after the work item is fetched so a dirty main checkout
    # can be judged against the description's Key Files section
    # (SA-0MV0NSOGE000OSMR). When the checkout is dirty but every dirty path
    # is demonstrably irrelevant to those Key Files, proceed to create a
    # clean worktree from HEAD and leave the dirty files untouched in the
    # main checkout. Anything uncertain — no Key Files section, or any
    # overlap — keeps the original abort-and-ask behaviour. The gate protects
    # the main checkout only: when invoked from inside a linked worktree
    # (e.g. resuming a driven child) it is skipped.
    if _current_checkout_is_linked_worktree():
        LOG.info(
            "Inside a linked worktree — skipping the main-checkout dirty-tree gate."
        )
        status_output = ""
        dirty_decision = {
            "dirty": False,
            "dirty_files": [],
            "key_files": [],
            "relevant_files": [],
            "can_proceed": True,
            "reason": "inside a linked worktree",
        }
    else:
        LOG.info("Checking git working tree...")
        status_output = git_status()
        dirty_decision = classify_dirty_tree_relevance(
            status_output,
            work_item.get("description", ""),
            expected_dirty=expected_dirty,
        )
    report["dirty_decision"] = {
        "dirty": dirty_decision["dirty"],
        "dirty_files": dirty_decision["dirty_files"],
        "key_files": dirty_decision["key_files"],
        "relevant_files": dirty_decision["relevant_files"],
        "can_proceed": dirty_decision["can_proceed"],
        "reason": dirty_decision["reason"],
    }

    if dirty_decision["dirty"] and not dirty_decision["can_proceed"]:
        msg = (
            f"Dirty working tree detected. Uncommitted changes exist outside .worklog/.\n"
            f"Do NOT stash, commit, or revert the user's uncommitted changes.\n"
            f"STOP and ask the operator how to proceed (commit, stash, revert, or abort)\n"
            f"— never touch the user's working tree without explicit permission.\n"
            f"Reason: {dirty_decision['reason']}.\n"
            f"\nTo abort and release the work item, run:\n"
            f"  python3 {Path(__file__).resolve()} abort {work_item_id}\n"
            f"\nGit status:\n{status_output}"
        )
        LOG.warning("Dirty working tree:\n%s", status_output)
        if not json_output:
            print("\n⚠  Dirty working tree detected")
            print("=" * 60)
            print(status_output)
            print("=" * 60)
            print("Ask the operator how to proceed — do not stash, commit, or revert user changes without permission.\n")
        report["success"] = False
        report["message"] = msg
        report["dirty_worktree"] = True
        wl_add_comment(work_item_id, f"Start phase aborted: dirty working tree detected.\n```\n{status_output}\n```")
        try:
            StatusLifecycle.update_status(work_item_id, "open", assignee="")
        except RuntimeError:
            LOG.error("Failed to reset work item %s status to open", work_item_id)
        if json_output:
            print(format_json_output(report))
        return report

    if dirty_decision["dirty"]:
        # Irrelevant dirty files: record the decision and the untouched paths
        # for auditability, then continue to create a clean worktree from HEAD
        # (AC1/AC4). No stash, commit, or revert is performed.
        LOG.warning(
            "Dirty working tree left untouched (files will not affect this work): %s",
            ", ".join(dirty_decision["dirty_files"]),
        )
        if not json_output:
            print("\nℹ  Dirty working tree left untouched — proceeding with a clean worktree")
            print("=" * 60)
            for path in dirty_decision["dirty_files"]:
                print(f"  • {path}")
            print(f"  Reason: {dirty_decision['reason']}")
            print("=" * 60)
        report["dirty_worktree"] = False
        report["dirty_worktree_left_behind"] = dirty_decision["dirty_files"]
        dirty_list = "\n".join(
            f"- `{path}`" for path in dirty_decision["dirty_files"]
        )
        wl_add_comment(
            work_item_id,
            "Start phase: dirty main checkout left untouched — proceeding to "
            "create a clean worktree from HEAD.\n"
            f"Reason: {dirty_decision['reason']}\n"
            f"Dirty files left behind in the main checkout:\n{dirty_list}",
        )

    # ── Step 6.1: Check risk/effort estimates — evaluate if missing ────
    # (SA-0MTTSWHQE0072N9J) plan_complete items without risk/effort now
    # trigger an automatic evaluation instead of failing the gate.
    LOG.info("Checking risk/effort estimates for %s...", work_item_id)
    re_result = _check_and_evaluate_risk_effort(work_item_id, work_item)
    report.setdefault("steps", {})["risk_effort"] = re_result
    if re_result.get("estimates_provided"):
        LOG.info(
            "Risk/effort evaluated: risk=%s effort=%s",
            re_result.get("risk"), re_result.get("effort"),
        )
        if not json_output:
            print(
                f"\nℹ  Risk/effort estimated: risk={re_result.get('risk')} "
                f"effort={re_result.get('effort')}\n",
            )
    elif not re_result.get("success", True):
        # Evaluation failed — log but do NOT block; the item can still be
        # implemented (the estimates will be missing but the gate is pass).
        LOG.warning(
            "Risk/effort evaluation failed for %s: %s — proceeding without estimates.",
            work_item_id, re_result.get("error", "unknown"),
        )

    # ── Step 6.5: Refresh parent branch before creating worktree (AC3) ─
    # Ensure the parent branch is up-to-date before forking a child
    # worktree. This prevents children from starting from a stale base
    # when another actor pushed to origin/<parent> between the last
    # finish and this start. Degrades gracefully (dirty/offline/diverged
    # → fall back to the local branch; never fails the start phase).
    # The refresh fast-forwards the SHARED local parent ref, so it takes the
    # same per-repository lock a concurrent ``finish`` holds
    # (SA-0MVPZG3IQ009IJMO) — bounded, fail-open: a held lock skips the
    # refresh rather than blocking or failing the start.
    repo_root = _get_repo_root() or str(Path.cwd().resolve())

    # ── Step 6.6: Fail-open self-referential-upstream warning (AC2/AC3) ─
    # A long-lived branch tracking itself makes `git pull` a silent no-op
    # (stale skills/worklog). Surfaced as a WARNING only: it never blocks a
    # work item and never rewrites config — repair is an explicit operator
    # action (SA-0MVX35AUC003CNUD, parent SA-0MVX1M6HH004YPX2).
    report["steps"] = report.get("steps", {})
    report["steps"]["self_referential_upstream"] = (
        warn_self_referential_upstream(repo_root)
    )

    LOG.info("Refreshing parent branch %s from origin...", parent_branch)
    refresh_result = _sync_parent_branch_locked(
        repo_root, parent_branch, work_item_id
    )
    report["steps"] = report.get("steps", {})
    report["steps"]["sync_parent_branch"] = refresh_result
    if refresh_result.get("warning"):
        LOG.info("Parent-branch refresh: %s", refresh_result["warning"])

    # ── Step 7: Create (or resume) worktree ────────────────────────
    # The default worktree path is repository-root-relative
    # (``worktree_path_for`` returns ``.worklog/worktrees/wl-<id>-<slug>``).
    # Resolve it against the **main checkout root** (``repo_root``, which
    # already falls back to cwd when ``_get_repo_root()`` returns ``None``),
    # never against cwd: when ``start`` is invoked from inside the item's own
    # linked worktree (a driven child resuming its pre-created tree, or a
    # human resuming after a crash) ``Path(wt_path).resolve()`` would
    # otherwise nest the path under the worktree root, ``_is_worktree()``
    # would miss the registered tree, and ``git worktree add`` would attempt
    # a nested creation that fails (SA-0MVXUGXS5006M3HF).
    #
    # ``--worktree-path`` stays authoritative with unchanged semantics: an
    # absolute override is used as given and a relative override resolves
    # against cwd (the historical behaviour, documented in SKILL.md).
    if worktree_path_override:
        wt_path = worktree_path_override
        abs_wt_path = str(Path(worktree_path_override).resolve())
    else:
        wt_path = worktree_path_for(work_item_id, slug)
        abs_wt_path = str((Path(repo_root) / wt_path).resolve())
    branch = branch_name_for(work_item_id, slug)
    # Idempotent start (SA-0MUWNHKQH0034DW9): when the worktree already
    # exists — e.g. the parent phase created it, or a driven session is
    # resuming — reuse it instead of failing on `git worktree add`. This lets
    # a fresh child session run the standard leaf workflow unchanged.
    resumed = _is_worktree(Path(abs_wt_path))

    if resumed:
        LOG.info(
            "Worktree already exists at %s — resuming implementation",
            abs_wt_path,
        )
    else:
        LOG.info("Creating worktree at %s from branch %s...", abs_wt_path, parent_branch)
        # Snapshot the parent's tracking config BEFORE the registration so the
        # post-add guard can distinguish "this registration poisoned the
        # parent" (a hard failure) from a pre-existing defect (detection's
        # job, never prevention's). The shared common config is read through
        # the main checkout (SA-0MVX35ABT000I1PD AC1).
        parent_tracking_before = (
            _git_config_get(repo_root, f"branch.{parent_branch}.remote"),
            _git_config_get(repo_root, f"branch.{parent_branch}.merge"),
        )
        if not git_worktree_add(branch, abs_wt_path, parent_branch):
            msg = f"Failed to create worktree at {abs_wt_path} from {parent_branch}"
            LOG.error(msg)
            report["success"] = False
            report["message"] = msg
            try:
                StatusLifecycle.update_status(work_item_id, "open", assignee="")
            except RuntimeError:
                LOG.error("Failed to reset work item %s status to open", work_item_id)
            if json_output:
                print(format_json_output(report))
            return report
        # Assert afterwards that the registration left the parent's tracking
        # config untouched: ``git worktree add --track`` must write ``.`` only
        # for the new wl-* branch, never onto a long-lived branch name
        # (SA-0MVX35ABT000I1PD AC1 — fail closed, loudly).
        try:
            assert_parent_tracking_untouched(
                repo_root, parent_branch, parent_tracking_before
            )
        except RuntimeError as exc:
            msg = str(exc)
            LOG.error("%s", msg)
            report["success"] = False
            report["message"] = msg
            try:
                StatusLifecycle.update_status(work_item_id, "open", assignee="")
            except RuntimeError:
                LOG.error("Failed to reset work item %s status to open", work_item_id)
            if json_output:
                print(format_json_output(report))
            return report

    # Materialise the worktree-local node_modules link farm (or, for repos
    # without a workspace package consumed by path, the previous
    # whole-directory symlink) so dist-spawning tests resolve dependencies
    # without manual setup, and report whether the shared workspace package
    # resolves inside the worktree. Never fatal: the link step stays
    # best-effort (SA-0MSGS763C006SM1B, SA-0MVPI400Y001FOM1).
    node_modules_report = _prepare_node_modules(abs_wt_path, _get_repo_root())
    report["steps"]["node_modules"] = node_modules_report
    report["shared_resolution_ok"] = node_modules_report["shared_resolution_ok"]
    report["shared_resolution"] = node_modules_report["shared_resolution"]
    resolution_check = node_modules_report["shared_resolution"]
    if resolution_check["applicable"] and not resolution_check["ok"]:
        LOG.warning(
            "Worktree %s does not resolve %s to its own %s (resolved: %s). "
            "A change under that package will be refused by the finish gate "
            "until the link step is refreshed (re-run `implement.py start` "
            "from the main checkout).",
            abs_wt_path,
            SHARED_WORKSPACE_PACKAGE_NAME,
            SHARED_WORKSPACE_PACKAGE_DIR.as_posix(),
            resolution_check["resolved_root"] or "<missing>",
        )

    # Nested/workspace packages keep their own node_modules (e.g. a pi package
    # under .pi/packages/<pkg>); symlink those too so tools run from the
    # worktree resolve dependencies. Best-effort: never fatal
    # (OSL-0MUFG3PJA00379CT).
    _ensure_nested_node_modules_symlinks(abs_wt_path, _get_repo_root())

    # Initialise git submodules in the worktree (git worktree add does not
    # do this automatically). Best-effort: a warning on failure, never fatal.
    _ensure_submodules(abs_wt_path, _get_repo_root())

    # The work item was already claimed (status-only) in Step 3; the stage
    # must be left untouched here. There is deliberately no second
    # status/stage update — passing the retired `in_progress` stage to `wl`
    # failed atomically and was only logged as a warning (SA-0MUY9PMB7001ENMP).
    report["resumed"] = resumed
    wl_add_comment(
        work_item_id,
        (
            f"Implementation resumed\n- Worktree: {abs_wt_path}\n- Branch: {branch}"
            if resumed
            else f"Implementation started\n- Worktree: {abs_wt_path}\n- Branch: {branch}"
        ),
    )

    # ── Step 8: Register signal handlers ───────────────────────────
    # ``repo_root`` is the main checkout root resolved at Step 6.5. Do NOT
    # recompute it from ``Path.cwd()``: on a resume from inside the worktree
    # ``cwd`` IS the worktree, so the state file and the signal globals
    # recorded the worktree — which then made finish refuse to remove "the
    # repository root" after a successful push (MS-0MVM7NJF70037YU4).
    _store_signal_globals(abs_wt_path, work_item_id, repo_root)
    _register_signal_handlers()

    # ── Step 9: Write state ────────────────────────────────────────
    state = ImplementState(
        work_item_id=work_item_id,
        worktree_path=abs_wt_path,
        repo_root=repo_root,
        parent_branch=parent_branch,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    write_state(state, abs_wt_path)

    report["worktree_path"] = abs_wt_path
    report["branch"] = branch
    report["title"] = title
    report["message"] = f"Worktree created at {abs_wt_path}. Switch to the worktree and implement."

    if json_output:
        print(format_json_output(report))
    else:
        print()
        print("=" * 60)
        print(f"  Implement: {title} ({work_item_id})")
        print("=" * 60)
        print(f"  Worktree:  {abs_wt_path}")
        print(f"  Branch:    {branch}")
        print(f"  Parent:    {parent_branch}")
        print()
        print("  Next steps:")
        print(f"  1. cd {abs_wt_path}")
        print("  2. Write tests and implementation code")
        print("  3. Run: python3 scripts/implement.py finish <id>")
        print()
        print("  To abort:")
        print(f"  python3 scripts/implement.py abort {work_item_id}")
        print()

    return report


def _recover_non_fast_forward_push(
    worktree_path: str,
    repo_root: str,
    branch: str,
    commit_hash: str,
    exc: PushNonFastForwardError,
    work_item_id: str,
    evidence_out: dict[str, Any] | None = None,
    parent_branch: str = DEFAULT_PARENT_BRANCH,
    ff_retry_out: dict[str, Any] | None = None,
) -> str:
    """Recover from a non-fast-forward push by rebasing and retrying.

    ``dev`` advanced concurrently, so the feature branch must be rebased
    onto the latest ``origin/dev`` and re-pushed.  The rebase MUST run
    inside the worktree (that is where *branch* is checked out);
    ``phase_finish`` therefore pushes *before* removing the worktree.

    ``origin/dev`` is fetched first: a failed push leaves the
    remote-tracking ref at the pre-advance tip, so rebasing onto the
    stale ref would just produce another non-fast-forward push.

    Args:
        worktree_path: Worktree where *branch* is checked out and where the
            rebased commit lives — the checkout the retry push runs from, so
            the dev-push gate's subject is the pushed tree.
        repo_root: Main checkout. Retained for call-site compatibility; the
            retry push itself runs from *worktree_path* (never from here).
        branch: Feature branch to rebase and push.
        commit_hash: Commit being pushed (pre-rebase).
        exc: The original :class:`PushNonFastForwardError`.
        work_item_id: Work item to comment on for the recovery audit trail.
        evidence_out: Optional dict populated on a successful retry with the
            gate evidence (passed through to :func:`git_push_to_dev`).
        parent_branch: Shared parent branch whose local checkout is retried
            after the rebase (default: ``dev``).
        ff_retry_out: Optional dict populated with the post-rebase
            fast-forward retry result, recorded by ``finish``
            (SA-0MW3H07A8004Q54C AC1).

    Returns:
        The (possibly rewritten) commit hash that was pushed.

    Raises:
        RuntimeError: if the rebase or the retry push fails; recovery
            state (commit, branch, worktree) is preserved in a comment
            before raising so a human can recover manually.
    """
    # 0. Refresh origin/dev so the rebase targets the concurrently-advanced
    #    tip rather than the stale remote-tracking ref.  Best-effort: if the
    #    fetch fails we still attempt the rebase and let the retry push
    #    surface the problem.
    fetch_result = run_cmd(["git", "fetch", "origin", "dev"], cwd=worktree_path)
    if fetch_result.returncode != 0:
        LOG.warning(
            "git fetch origin dev failed during non-fast-forward recovery: %s",
            fetch_result.stderr.strip(),
        )

    # 1. Rebase the feature branch onto the latest origin/dev.
    rebase_result = run_cmd(["git", "rebase", "origin/dev"], cwd=worktree_path)
    if rebase_result.returncode != 0:
        # AC3: rebase failed — preserve all state for manual recovery.
        wl_add_comment(
            work_item_id,
            f"Push to dev failed (non-fast-forward): {exc.stderr}\n"
            f"Rebase onto origin/dev failed with conflicts.\n"
            f"Manual recovery:\n"
            f"  1. cd {worktree_path}\n"
            f"  2. git rebase origin/dev  # resolve conflicts\n"
            f"  3. git push origin {branch}:refs/heads/dev\n"
            f"Commit before rebase: {commit_hash}\n"
            f"Branch: {branch}\n"
            f"Worktree: {worktree_path}",
        )
        raise RuntimeError(
            f"git push to dev failed (non-fast-forward), rebase "
            f"also failed: {exc.stderr}",
        ) from exc

    # The rebase rewrote HEAD — capture the new tip so the retry push and
    # the completion comment reference the commit that actually lands.
    new_commit = git_get_commit_hash(worktree_path) or commit_hash

    # AC1 (SA-0MW3H07A8004Q54C): Step 5's fast-forward was refused (or pointed
    # at the pre-rebase commit) because dev had moved. The rebase now yields a
    # commit based on origin/dev, so retry the fast-forward *here* — the main
    # checkout's dev (and its HEAD when dev is checked out) then tracks the
    # pushed commit. Best-effort by contract: a second refusal is recorded and
    # never fails the finish, exactly like Step 5.
    ff_retry = _fast_forward_local_dev_for_push(
        repo_root, new_commit, parent_branch
    )
    if ff_retry_out is not None:
        ff_retry_out.clear()
        ff_retry_out.update(ff_retry)
    LOG.info(
        "Post-rebase local %s fast-forward retry: method=%s advanced=%s",
        parent_branch,
        ff_retry.get("method"),
        ff_retry.get("advanced"),
    )

    # 2. Retry the push from the worktree that now holds the rebased commit —
    #    never from the main checkout, which still holds the pre-rebase commit
    #    and would make the dev-push gate validate the wrong tree
    #    (SA-0MVTFMB7K002Y1F3 AC1/AC2). The subject guard fails closed when
    #    the worktree cannot hold the rebased commit.
    try:
        _assert_push_subject(worktree_path, new_commit, branch, worktree_path)
    except PushSubjectMismatchError as mismatch:
        wl_add_comment(
            work_item_id,
            f"Retry dev push refused after the recovery rebase "
            f"(push-subject mismatch): {mismatch}\n"
            f"Branch: {branch}\nCommit: {new_commit}\n"
            f"Worktree: {worktree_path}",
        )
        raise RuntimeError(
            f"git push to dev failed after rebase (push-subject mismatch): "
            f"{mismatch}"
        ) from mismatch
    if not git_push_to_dev(
        worktree_path, branch, new_commit, evidence_out=evidence_out
    ):
        # Retry failed — preserve state for manual recovery.
        wl_add_comment(
            work_item_id,
            f"Push to dev failed after rebase.\n"
            f"Original error: {exc.stderr}\n"
            f"Branch: {branch}\n"
            f"Commit: {new_commit}\n"
            f"Worktree: {worktree_path}",
        )
        raise RuntimeError(
            f"git push to dev failed after rebase: {exc.stderr}",
        ) from exc

    # 3. Post explanatory comment about the successful recovery.
    wl_add_comment(
        work_item_id,
        f"Push to dev initially failed (non-fast-forward \u2014 dev "
        f"advanced concurrently). Rebased {branch} onto origin/dev "
        f"and retried successfully. Original error: {exc.stderr}",
    )
    return new_commit


def phase_finish(
    work_item_id: str,
    json_output: bool = False,
    no_refactor: bool = False,
    commit_msg_override: str | None = None,
    max_retry: int = DEFAULT_MAX_RETRY,
    verbose: bool = False,
    keep_worktree: bool = False,
) -> dict[str, Any]:
    """Phase 2: Complete the implementation.

    Steps:
    0. Worktree placement gate (refuse if changes were made outside the worktree)
    1. Read state to find worktree
    2. Refactor step (unless --no-refactor)
    3. Build
    3b. Stale shared-resolution gate: refuse when the change set touches
        ``packages/shared/src/**`` while ``@worklog/shared`` still resolves
        outside this worktree (SA-0MVPI41SR008JYXL)
    4. Test (with fix-and-re-run loop)
    5. Commit
    6. Verified, reversible fast-forward of dev in the main checkout
       (context-budget fix; restored to origin/dev on push failure)
    7. Push to dev (rebase + retry on concurrent advance)
    8. Post-push ancestry assertion: the pushed commit must be an ancestor
       of refs/remotes/origin/dev or finish fails loudly naming the hash
       (SA-0MVPZG7A0000N66U AC2)
    9. Post-push full-suite verification (**read-only** — it never starts a
       suite): resolve the full-suite verdict for the pushed tree from the
       test cache. ``green`` / ``not_applicable`` permit the terminal
       transition; ``red`` / ``missing`` refuse it — the item is released to
       ``open`` with the pushed commit named, retained and never reverted,
       and the failure is routed to triage (SA-0MVXUSJ2P009T97H AC4)
    10. Clean up worktree processes (post-push, before removal —
       self-exclusion-safe, SA-0MVJC5KNT0044L2B)
    11. Remove worktree — **deferred** when the finishing process belongs to
        a driven child session (``IMPLEMENT_DRIVE_ACTIVE=1``) or when the
        caller opted in with ``--keep-worktree`` /
        ``IMPLEMENT_KEEP_WORKTREE=1``. Deferral keeps the tree, its branch
        and ``.implement_state.json`` so the session that owns the tree as
        its cwd keeps working after finish returns; the driver (or the
        caller, via ``implement.py cleanup <id>`` as its **last** bash
        command, SA-0MVTHJ7QX005PXRG) removes it afterwards. That cleanup
        is explicitly **not** a remaining lifecycle step of this run —
        ownership passed to the driver/caller, and it can no longer change
        the work item. Recorded in ``report["steps"]["worktree_removal"]``
        (SA-0MVTE8XAJ002IUDR)
    12. Sync local parent branch (post-push, AC1/AC2)
    13. Completion comment (commit hash, branch, gate provenance, retained
        tree) — **before** the terminal write, so no comment follows it and
        the audit-freshness ordering rule is preserved (SA-0MTHC710X003ORZM)
    14. Terminal write (``completed``/``in_review`` + assignee release) issued
        by ``StatusLifecycle.__exit__`` — the run's **last lifecycle
        action** (design A, SA-0MVXUSED6008J04R)
    15. Read back the terminal status from the main-checkout store and
        fail loudly if it did not stick (OSL-0MVME1PI5005WU58)

    Every step that can still change the work item — including the
    verification, the completion comment and the (non-deferred) worktree
    removal — runs inside the held, non-terminal ``StatusLifecycle`` window,
    so the item never becomes an audit-dispatch candidate while the run is
    unfinished (parent SA-0MVWFPI31000HP3H AC1/AC2; documented by
    SA-0MVXUSK02003ZMDN, pinned by
    ``tests/test_finish_finalisation_ordering.py``).

    **Interruption and re-run (SA-0MVXUSJJY009HMQ0).** Every exit that fails
    after the commit — the classified ``RuntimeError`` branches, the
    unexpected-exception branch and the SIGTERM/SIGINT cleanup — releases the
    item (``open``, empty assignee) and records the retained state once per
    commit: the published (or local) commit, the branch, whether the worktree
    survives and the resume command. Re-running ``finish`` then converges to
    ``completed`` / ``in_review`` through the ordinary no-diff/no-op-push
    path, and the completion comment (Step 9) is deduplicated on the marker
    plus commit hash — so a recovered interruption never leaves a duplicate
    completion comment or a second terminal transition in one run.

    All implementation work MUST be done inside the worktree created by
    ``phase_start``. If the current directory is outside the worktree and the
    main checkout holds uncommitted changes, finish refuses (step 0).

    Args:
        work_item_id: The work item ID.
        json_output: If True, output JSON.
        no_refactor: If True, skip refactor step.
        commit_msg_override: Custom commit message.
        max_retry: Max test-fix retries.
        verbose: Enable verbose logging.
        keep_worktree: Defer worktree removal (``--keep-worktree``); the
            caller then owns the cleanup so the session keeps its cwd.

    Returns:
        Dict with result information.
    """
    report: dict[str, Any] = {
        "phase": "finish",
        "work_item_id": work_item_id,
        "success": True,
        "steps": {},
        "message": "",
    }
    # A stale context from an earlier run in this process must never be used to
    # describe this run's interruption (SA-0MVXUSJJY009HMQ0 AC2).
    _clear_finish_progress()

    # ── Step 0: Find worktree (from state or directory scan) ──────
    worktree_path = _discover_worktree(work_item_id)
    if not worktree_path:
        msg = (
            f"Cannot find worktree for {work_item_id}. "
            f"Run `python3 scripts/implement.py start {work_item_id}` first."
        )
        report["success"] = False
        report["message"] = msg
        if json_output:
            print(format_json_output(report))
        else:
            LOG.error(msg)
        return report

    # Read state for metadata
    state = read_state(worktree_path)
    # The shared parent branch this finish syncs and verifies against (dev).
    # Resolved once here so the verified fast-forward (Step 5), the restore
    # (Step 6) and the ancestry assertion (Step 6a) all use the same ref.
    parent_branch = state.parent_branch if state else DEFAULT_PARENT_BRANCH

    report["worktree_path"] = worktree_path
    # Compute branch name from the worktree
    branch = Path(worktree_path).name  # e.g., wl-SA-xxx-slug

    # Check we're in the right directory
    if not Path(worktree_path).exists():
        msg = f"Worktree directory does not exist: {worktree_path}"
        report["success"] = False
        report["message"] = msg
        if json_output:
            print(format_json_output(report))
        else:
            LOG.error(msg)
        return report

    # ── Step 0.5: Worktree placement gate ──────────────────────────
    # All implementation work must happen inside the worktree created by
    # `implement.py start`. If changes were made in the main checkout
    # instead, refuse rather than silently build/test/commit an empty
    # worktree and mark the item in_review.
    violation = _worktree_placement_violation(
        worktree_path,
        parent_branch=state.parent_branch if state else DEFAULT_PARENT_BRANCH,
    )
    if violation:
        msg = violation
        LOG.error(msg)
        report["success"] = False
        report["message"] = msg
        report["worktree_enforcement"] = True
        # AC5 (LP-0MVWEP1MM0097RAT): the placement violation records a
        # structured step so the classifier resolves it as a gate failure
        # without parsing prose (``worktree_placement`` + any reason).
        report["steps"]["worktree_placement"] = {
            "success": False,
            "reason": "placement_violation",
        }
        wl_add_comment(
            work_item_id,
            "Finish phase refused: implementation changes found outside the "
            f"worktree.\n```\n{msg}\n```",
        )
        try:
            StatusLifecycle.update_status(work_item_id, "open", assignee="")
        except RuntimeError:
            LOG.error("Failed to reset work item %s status to open", work_item_id)
        if json_output:
            print(format_json_output(report))
        else:
            print(f"\n⛔ {msg}\n")
        return report

    # ── Step 0.75: Main-checkout guard (WL-0MVPADZ1Z000FOSG AC3) ──
    # A worktree-scoped finish must never silently build on, fast-forward
    # over, or push foreign tracked-file mutations of the main checkout. All
    # implementation work happens in the worktree, but finish itself operates
    # on the main checkout (Step 5 fast-forward, Step 6 push, Step 7
    # removal), so the main checkout is snapshotted now and re-verified
    # before the push and before worktree removal. A mutation not
    # attributable to finish's own fast-forward aborts loudly, names every
    # offending path, and leaves the item open.
    # ``state.repo_root`` is the main checkout root, but a state file written
    # by an older build may carry the worktree itself (start resumed from
    # inside it); such a stale value is rejected and the main root re-derived
    # from the worktree (MS-0MVM7NJF70037YU4).
    repo_root = _resolve_main_repo_root(worktree_path, state)
    main_checkout_baseline = _snapshot_main_checkout(repo_root)
    main_checkout_guard: dict[str, Any] = {
        "repo_root": repo_root,
        "snapshot_at": main_checkout_baseline["taken_at"],
        "baseline_paths": len(main_checkout_baseline["status"]),
        "own_churn": [],
        "verifies": [],
        "verdict": "pass",
    }
    report["steps"]["main_checkout_guard"] = main_checkout_guard

    def _abort_on_main_checkout_violation(stage: str, own_churn: list[str]) -> None:
        """Verify the main checkout; raise a loud abort when it was mutated.

        Records the verdict in ``report["steps"]["main_checkout_guard"]``
        and, on failure, comments on the work item before raising so the
        audit trail survives the status reset in the outer handler.
        """
        violations = _verify_main_checkout(
            main_checkout_guard,
            main_checkout_baseline,
            repo_root=repo_root,
            worktree_path=worktree_path,
            own_churn=own_churn,
            expected_dirty=expected_dirty_paths(repo_root),
            stage=stage,
        )
        if not violations:
            return
        actor = main_checkout_guard["verifies"][-1].get(
            "actor_evidence", "unknown"
        )
        msg = _main_checkout_guard_message(
            repo_root, violations, actor, stage
        )
        wl_add_comment(work_item_id, msg)
        raise RuntimeError(msg)

    def _record_main_checkout_verify(
        stage: str, *, aborting: bool
    ) -> list[str]:
        """Record a guard verify on a pre-push abort or advisory path.

        ``finish`` snapshots the main checkout at Step 0.75 but originally
        verified only at ``pre_push`` and ``pre_worktree_removal``. Every
        pre-push abort (build failure, gate failure in JSON mode, retries
        exhausted, interactive user abort) returned before either point, so
        ``verifies`` stayed ``[]`` and foreign deletions in the main checkout
        went unreported (2026-10-31: 37 deletions, ``verifies: []``). This
        runs the verify at *stage* (one of :data:`VERIFY_STAGES`) and, on a
        violation, posts the loud guard message — naming every offending path,
        the actor evidence and the recovery command — **in addition to** any
        pre-existing failure comment, never instead of it (fail loud, never
        silent).

        ``aborting=False`` is the report-only refactor path: ``run_refactor``
        failure is advisory today (``LOG.warning`` + continue), so a violation
        is surfaced in the report and a wl comment but the run is **not**
        aborted. Turning a pre-existing advisory step into a new abort source
        would risk the SA-0MUIJAHXS003M7BG false-positive class.

        The operator-facing contract — stage table, report fields, the
        both-comments ordering and the advisory refactor status — is
        documented in ``docs/dev/implement-skill-reference.md``
        (§ Finish main-checkout guard), and
        ``skill/implement/tests/test_guard_docs.py`` keeps the two in sync.

        Returns:
            The offending paths (empty list on pass).
        """
        violations = _verify_main_checkout(
            main_checkout_guard,
            main_checkout_baseline,
            repo_root=repo_root,
            worktree_path=worktree_path,
            own_churn=main_checkout_guard["own_churn"],
            expected_dirty=expected_dirty_paths(repo_root),
            stage=stage,
        )
        if not violations:
            return violations
        actor = main_checkout_guard["verifies"][-1].get(
            "actor_evidence", "unknown"
        )
        msg = _main_checkout_guard_message(repo_root, violations, actor, stage)
        if not aborting:
            msg = (
                "Advisory only — this guard verify does not abort the run "
                "(refactor step):\n\n" + msg
            )
        wl_add_comment(work_item_id, msg)
        return violations

    # ── Step 1: Refactor step ──────────────────────────────────────
    if not no_refactor:
        LOG.info("Running refactor step...")
        refactor_result = run_refactor(work_item_id, worktree_path)
        report["steps"]["refactor"] = refactor_result
        if not refactor_result["success"]:
            # Report-only guard verify (SA-0MVXVBYXI000QMXM): refactor
            # failure is advisory (warning + continue), so the guard observes
            # the main checkout here but never aborts on it. Keeps the guard
            # honest without making a pre-existing advisory step a new abort
            # source (false-positive risk under parent AC5).
            LOG.warning("Refactor step reported issues; continuing workflow")
            _record_main_checkout_verify("refactor", aborting=False)
    else:
        report["steps"]["refactor"] = {"skipped": True, "reason": "no_refactor_flag"}
        LOG.info("Refactor step skipped (--no-refactor)")

    # ── Step 2: Build ──────────────────────────────────────────────
    # The item's own change set is snapshotted before the build: whatever the
    # build changes on top of it is build-originated churn, not the item's
    # change (SA-0MVZE164P0048306 AC1/AC3). A build such as ``npm run build``
    # here runs ``engine:sync`` first, which can advance the engine submodule
    # and stage the pin plus the gitlink.
    build_churn_baseline = _build_churn_baseline(worktree_path)
    LOG.info("Running build...")
    build_result = run_build(worktree_path)
    if not build_result["success"]:
        # A failed build may itself have advanced the engine past the item's
        # pin (``engine:sync`` runs first), so the compile errors may come
        # from engine APIs this item predates. The lag is reported — additively
        # — in the report and the failure comment (SA-0MW06FLFR009F0P4).
        build_result["engine_lag"] = _engine_lag(
            worktree_path, build_churn_baseline
        )
    report["steps"]["build"] = build_result
    if not build_result["success"]:
        msg = f"Build failed (exit code {build_result['exit_code']})"
        LOG.error(msg)
        if build_result["stderr"]:
            LOG.error("Build stderr:\n%s", build_result["stderr"][:2000])
        if build_result["engine_lag"].get("advanced"):
            LOG.error(
                "Build engine lag:\n%s",
                _engine_lag_diagnosis(build_result["engine_lag"]),
            )
        report["success"] = False
        report["message"] = msg
        wl_add_comment(
            work_item_id,
            _build_failure_message(build_result, build_result["engine_lag"]),
        )
        # The failure comment is posted above; this appends the failure-path
        # guard verify (and, on foreign churn, its own loud comment) so a
        # pre-push abort never leaves ``verifies`` empty (SA-0MVXVBYXI000QMXM).
        _record_main_checkout_verify("build_failure", aborting=True)
        try:
            StatusLifecycle.update_status(work_item_id, "open", assignee="")
        except RuntimeError:
            LOG.error("Failed to reset work item %s status to open", work_item_id)
        if json_output:
            print(format_json_output(report))
        return report

    # ── Step 2b: Exclude build-originated composition churn ────────
    # Build output the item did not originate must never reach the commit
    # (AC1) and must not be what the suite runs against (AC3): both are
    # restored to the pre-build ``HEAD`` state while the gate is still ahead
    # of it, so the tested tree is exactly the committed tree and the composed
    # engine SHA is the committed one. The exclusion is conditional on origin
    # (AC2): a pin/gitlink that was already part of the item's change set is
    # preserved untouched.
    build_churn = _exclude_build_originated_churn(
        worktree_path, build_churn_baseline
    )
    report["steps"]["build_churn"] = build_churn
    if build_churn.get("restore_failed"):
        msg = _build_churn_refusal_message(build_churn)
        LOG.error(msg)
        report["success"] = False
        report["message"] = msg
        wl_add_comment(work_item_id, msg)
        try:
            StatusLifecycle.update_status(work_item_id, "open", assignee="")
        except RuntimeError:
            LOG.error("Failed to reset work item %s status to open", work_item_id)
        if json_output:
            print(format_json_output(report))
        return report

    # ── Step 2c: Stale shared-resolution gate ─────────────────────
    # The gate runs against the worktree but `@worklog/shared` (and every
    # other path-consumed workspace package) resolves through the worktree's
    # `node_modules` link farm. When that farm is missing or stale, the
    # package resolves to the MAIN CHECKOUT's build output: a correct shared
    # change false-reds (WL-0MVK6S4HM005GBCO) and a broken one can
    # false-green. Once the change set touches `packages/shared/src/**` a
    # stale resolution means the gate cannot see the change at all, so it
    # fails closed here (before the test gate, after the build) rather than
    # reporting a verdict about the wrong code (SA-0MVPI41SR008JYXL, parent
    # AC4). Repos without a workspace shared package are a no-op.
    stale_resolution = _stale_shared_resolution(
        worktree_path, repo_root, parent_branch=parent_branch
    )
    report["steps"]["shared_resolution"] = {
        **_shared_resolution_check(worktree_path, repo_root),
        "refused": stale_resolution is not None,
    }
    if stale_resolution:
        LOG.error("Stale shared resolution — refusing to run the gate")
        report["success"] = False
        report["message"] = stale_resolution
        wl_add_comment(work_item_id, stale_resolution)
        try:
            StatusLifecycle.update_status(work_item_id, "open", assignee="")
        except RuntimeError:
            LOG.error("Failed to reset work item %s status to open", work_item_id)
        if json_output:
            print(format_json_output(report))
        else:
            print(f"\n⛔ {stale_resolution}\n")
        return report

    # ── Step 3: Test with fix-and-re-run loop ──────────────────────
    # The suite runs against the **would-be-commit tree**: the change set is
    # staged into a temporary copy of the index and exported as
    # ``GIT_INDEX_FILE``, so tracked-file guards (``git ls-files``) see exactly
    # what the commit will produce (SA-0MVQHVGFR006W8LG). The real index and
    # the working tree are not touched until the gate is green.
    LOG.info("Running test suite (changed scope, would-be-commit tree)...")
    test_attempts = 0
    test_result, gate_tree = run_gate_suite(worktree_path, scope="changed")
    report["steps"]["would_be_commit"] = gate_tree
    report["steps"]["tests"] = []
    report["steps"]["tests"].append(
        _gate_attempt_record(test_attempts + 1, test_result)
    )

    while not test_result["success"] and test_attempts < max_retry:
        test_attempts += 1
        failures = test_result.get("failures", [])
        failure_reason = _gate_failure_kind(test_result)
        # The message always explains WHY the gate failed — slot contention
        # and unparsed output included, never an empty `Failures:` block
        # (WL-0MVGUT787000LNDG).
        msg = _gate_failure_message(
            test_result, test_attempts, max_retry, work_item_id
        )
        LOG.warning(
            "Test run %d/%d failed (%s)",
            test_attempts,
            max_retry,
            failure_reason,
        )

        if json_output:
            # Report current state and let the agent loop externally
            report["success"] = False
            report["message"] = msg
            # The report states the classification *and* the facts that
            # decided it (SA-0MVTET8KQ0031R6Z): a consumer never has to
            # re-derive the configured timeout, the execution time, the
            # separate queue wait, the command or the exit code/signal from
            # prose. Additive keys only — an older consumer ignores them.
            report["test_phase"] = {
                "attempt": test_attempts,
                "max_retry": max_retry,
                "failures": failures,
                "notices": list(test_result.get("notices") or []),
                "notice_codes": list(test_result.get("notice_codes") or []),
                "reason": failure_reason,
                **_gate_facts(test_result, _gate_exit_code(test_result)),
            }
            # Failure-path guard verify: the JSON-mode early return aborts
            # before the push, so record the verify (and, on foreign churn,
            # post the loud guard comment) before reporting
            # (SA-0MVXVBYXI000QMXM). The gate failure itself stays in the
            # report — the guard comment never replaces it.
            _record_main_checkout_verify("gate_failure", aborting=True)
            print(format_json_output(report))
            return report

        # Interactive mode: prompt user to fix
        print()
        print("=" * 60)
        print(f"  ⚠  Test run {test_attempts}/{max_retry} failed")
        print("=" * 60)
        for line in _gate_failure_detail(test_result).splitlines():
            print(f"  {line}")
        print()
        print("  Fix the failures and press Enter to re-run tests.")
        print("  Type 'abort' to abort the finish phase, or 'skip' to skip tests.")
        try:
            choice = input("  [Enter/skip/abort]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "abort"

        if choice == "abort":
            msg = f"Aborted by user during test fix loop (attempt {test_attempts}/{max_retry})"
            report["success"] = False
            report["message"] = msg
            wl_add_comment(work_item_id, msg)
            # Failure-path guard verify after the pre-existing abort comment
            # (both comments, in order) — SA-0MVXVBYXI000QMXM.
            _record_main_checkout_verify("user_abort", aborting=True)
            try:
                StatusLifecycle.update_status(work_item_id, "open", assignee="")
            except RuntimeError:
                LOG.error("Failed to reset work item %s status to open", work_item_id)
            if json_output:
                print(format_json_output(report))
            else:
                print(f"\n{msg}")
            return report

        if choice == "skip":
            LOG.warning("Tests skipped by user choice")
            test_result["success"] = True
            report["steps"]["tests"][-1]["skipped"] = True
            break

        # Re-run tests
        LOG.info("Re-running test suite (attempt %d/%d, changed scope)...",
                 test_attempts + 1, max_retry)
        test_result, gate_tree = run_gate_suite(worktree_path, scope="changed")
        report["steps"]["would_be_commit"] = gate_tree
        report["steps"]["tests"].append(
            _gate_attempt_record(test_attempts + 1, test_result)
        )

    if not test_result["success"]:
        msg = (
            f"Tests failed after {max_retry} retries.\n\n"
            f"{_gate_failure_detail(test_result)}\n\n"
            "Aborting finish phase."
        )
        report["success"] = False
        report["message"] = msg
        wl_add_comment(work_item_id, msg)
        # Failure-path guard verify after the pre-existing retries-exhausted
        # comment (both comments, in order) — SA-0MVXVBYXI000QMXM.
        _record_main_checkout_verify("gate_failure", aborting=True)
        try:
            StatusLifecycle.update_status(work_item_id, "open", assignee="")
        except RuntimeError:
            LOG.error("Failed to reset work item %s status to open", work_item_id)
        if json_output:
            print(format_json_output(report))
        else:
            LOG.error(msg)
        return report

    # Working-tree churn attributed to a concurrent session is excluded from
    # the guard's verdict but is never silent: surface the note in the finish
    # output and the JSON report so the operator can tell an excluded foreign
    # change from a clean run (SA-0MUIJAHXS003M7BG AC3).
    excluded_churn = list(test_result.get("live_repo_excluded") or [])
    if excluded_churn:
        report["live_repo_excluded"] = excluded_churn
        for note in excluded_churn:
            LOG.info("Live-repo guard note: %s", note)

    # ── Step 3b: Full-suite coverage moved to the dev-push gate ─────
    # (SA-0MVECQUGF0064U81 — defer full-suite testing to integration.) No
    # full suite runs here: the changed-scope validation above is the
    # development-time (pre-commit) gate, kept fast so finish holds a
    # host-wide "test" slot only for the affected tests. The FULL suite
    # first runs at the dev-push integration gate — the pre-push hook
    # (`.githooks/pre-push`) executes `run_tests.py --scope full
    # --strict-git-env` on pushes to dev/main, and the ship release gate
    # re-runs it on the merged dev→main tree — and that run populates the
    # main-checkout `.worklog/cache` that post-push verification and the
    # audit skill consume read-only. Running the full suite here instead
    # would (a) burn a slot for the whole suite duration per work item,
    # (b) record a verdict no later consumer could safely reuse — before the
    # gate was tree-bound it was cached under a pre-commit dirty git-state
    # fingerprint (the verdict is now keyed by the would-be-commit tree SHA,
    # SA-0MVQHVHXI007PQAB, but the dev-push gate still runs the full suite on
    # the pushed commit), and (c) duplicate the push gate.
    LOG.info("Changed-scope validation passed — full suite deferred to the dev-push gate")

    # ── Step 3c: Single-owner main-checkout lock (WL-0MVPADZ1Z000FOSG AC5) ─
    # Everything from the commit onwards operates on the main checkout, so
    # exactly one finish per checkout may hold this critical section at a
    # time. The lock is an advisory flock — no measurable latency when free,
    # kernel-released when the holder dies, and recorded in the report for
    # observability. The wait is bounded by ``IMPLEMENT_FINISH_LOCK_TIMEOUT``
    # (default 0 = fail fast), so a concurrent finish never hangs this one.
    lock_timeout = _finish_lock_timeout()
    lock_fd, lock_refusal = acquire_main_checkout_lock(
        repo_root, work_item_id, timeout=lock_timeout
    )
    report["steps"]["main_checkout_lock"] = {
        "lock_path": str(_main_checkout_lock_path(repo_root)),
        "acquired": lock_fd is not None,
        "acquired_at": (
            datetime.now(timezone.utc).isoformat() if lock_fd is not None else None
        ),
        "timeout_seconds": lock_timeout,
        "holder": lock_refusal if lock_fd is None else None,
    }
    if lock_fd is None:
        # The helper's refusal is already actionable: it names the lock path,
        # the holder and the recovery step, and states the bounded wait that
        # expired when a timeout was configured.
        msg = lock_refusal or (
            f"Another run holds the main-checkout lock "
            f"({report['steps']['main_checkout_lock']['lock_path']}). "
            f"Aborting without modifying the checkout or the work item — "
            f"re-run finish once the holder completes."
        )
        report["success"] = False
        report["message"] = msg
        if json_output:
            print(format_json_output(report))
        else:
            LOG.error(msg)
        return report

    try:
        # Assignee release is the helper's default, but it only ever clears an
        # assignee the context manager holds — so finish declares the agent
        # claim it owns here and releases it with the terminal
        # ``completed``/``in_review`` transition. A producer hold on the item
        # is therefore never wiped by finishing it (SA-0MVVF6OJY000JZBL).
        with StatusLifecycle(
            work_item_id,
            assignee=os.environ.get("IMPLEMENT_AGENT_NAME") or "implement",
            target_stage="in_review",
        ):
            # ── Step 3d: Prove the real index matches the tested tree ─────
            # The gate ran against a temporary index. Stage the change set in
            # the real index exactly as ``git_commit`` does and assert its
            # ``write-tree`` equals the tree the suite ran against, so no
            # commit can be created for a tree that was never tested. On a
            # mismatch ``stage_tested_tree`` unstages again and raises below,
            # leaving the abort contract intact (SA-0MVQHVGFR006W8LG).
            if gate_tree.get("available", True):
                staged = stage_tested_tree(worktree_path, gate_tree.get("tree"))
            else:
                # No repository, no commit tree: the gate recorded why the
                # verification was unavailable (SA-0MVVKJGKP003QVNM).
                staged = {
                    "success": True,
                    "skipped": True,
                    "verified": False,
                    "reason": gate_tree.get("reason"),
                    "notice": gate_tree.get("notice", ""),
                }
            report["steps"]["would_be_commit_assertion"] = staged
            if not staged["success"]:
                raise RuntimeError(staged["message"])
            if staged.get("skipped"):
                LOG.warning(
                    "Finish gate tree verification skipped: %s",
                    staged.get("notice", ""),
                )
            else:
                LOG.info(
                    "Finish gate tree verified: committing tree %s",
                    staged["tree"],
                )

            # ── Step 4: Commit ─────────────────────────────────────────────
            commit_msg = commit_msg_override or f"{work_item_id}: Implementation complete"
            LOG.info("Committing changes...")
            if not git_commit(worktree_path, commit_msg):
                # AC5 (LP-0MVWEP1MM0097RAT): structured steps entry so the
                # classifier never needs the ``report["message"]`` prose.
                report["steps"]["commit"] = {
                    "success": False,
                    "reason": "commit_failed",
                }
                raise RuntimeError("git commit failed")

            commit_hash = git_get_commit_hash(worktree_path)
            report["steps"]["commit"] = {
                "hash": commit_hash,
                "message": commit_msg,
            }
            LOG.info("Committed at %s", commit_hash)
            # Interruption context (SA-0MVXUSJJY009HMQ0 AC2): a signal from
            # here on names the committed (and, after Step 6, published) work
            # in its retained-state record instead of releasing the item
            # unexplained. ``pushed`` is set once Step 6 confirms the push.
            _set_finish_progress(
                work_item_id=work_item_id,
                worktree_path=worktree_path,
                branch=branch,
                commit=commit_hash,
                pushed=False,
            )

            # ── Step 5: Verified, reversible local-dev fast-forward ───────
            # Main-checkout hygiene only — never the dev-push gate's subject.
            # Bring the main checkout's local dev branch up to the new commit
            # so the next worktree forks from an up-to-date base. The dev push
            # itself runs from the worktree holding the commit (Step 6), so
            # the pre-push hook measures the pushed tree, not this checkout
            # (SA-0MVTFMB7K002Y1F3 AC5).
            # ``repo_root`` was resolved (stale state rejected) at Step 0.75.
            #
            # The advance is *loud and reversible* (SA-0MVPZG7A0000N66U AC1):
            # it is recorded in the report, undoable on push failure (restore
            # to refs/remotes/origin/dev), and refuses loudly — naming the
            # refs and hashes — instead of the previous silent
            # ``except Exception`` whenever it cannot fast-forward safely.
            # The commit is inherently unpublishable at this point, so the
            # post-push ancestry assertion (Step 6a) is the authority on
            # whether it actually landed.
            LOG.info(
                "Fast-forwarding dev in main checkout (%s) to %s ...",
                repo_root,
                commit_hash,
            )
            local_dev_fast_forward = _fast_forward_local_dev_for_push(
                repo_root, commit_hash, parent_branch
            )
            report["steps"]["local_dev_fast_forward"] = local_dev_fast_forward
            main_checkout_guard["own_churn"] = list(
                local_dev_fast_forward.get("own_churn") or []
            )

            # ── Step 5b: Main-checkout guard verify (pre-push) ───────────
            # A foreign mutation of the main checkout must abort before the
            # push, never be carried along by finish's own fast-forward and
            # push.
            _abort_on_main_checkout_violation(
                "pre_push", main_checkout_guard["own_churn"]
            )

            # ── Step 6: Push to dev ────────────────────────────────────────
            # The push runs *before* the worktree is removed: a
            # non-fast-forward recovery must rebase the feature branch where
            # it is checked out (the worktree), and a failed push must leave
            # the worktree in place for manual recovery
            # (WL-0MV533YSW004D1E1).
            # repo_root was resolved in Step 5 (fast-forward) above.
            LOG.info(
                "Pushing to dev from the worktree that holds the pushed "
                "commit (%s)...",
                worktree_path,
            )
            # Evidence captured by the push (AC3/AC5): the gate's resolved
            # tree and executed-vs-cached verdict, exposed in --json output.
            push_evidence: dict[str, Any] = {}
            try:
                # AC1/AC2: the dev push runs from the checkout that holds the
                # pushed commit (the worktree), so ``.githooks/pre-push``
                # resolves its ``--project-root`` to the pushed tree. The
                # guard refuses before any push when that checkout cannot be
                # the subject, instead of silently gating a different tree.
                _assert_push_subject(
                    worktree_path, commit_hash, branch, worktree_path
                )
                if not git_push_to_dev(
                    worktree_path,
                    branch,
                    commit_hash,
                    evidence_out=push_evidence,
                ):
                    gate_reason = push_evidence.get("gate_reason")
                    if gate_reason:
                        # AC2: the hook named *why* (slot contention, timeout,
                        # killed, red suite, ...); report that reason rather
                        # than a generic push failure.
                        raise PushGateFailureError(
                            str(gate_reason),
                            str(push_evidence.get("stderr_tail") or ""),
                            commit_hash,
                            branch,
                        )
                    raise RuntimeError("git push to dev failed.")
            except PushSubjectMismatchError as exc:
                # Fail closed (AC2): nothing was pushed. Restore local dev off
                # the unpushed commit and preserve recovery state in a comment
                # so a human can materialise the pushed tree and retry.
                report["steps"]["push"] = {
                    "success": False,
                    "reason": "subject_mismatch",
                    "hash": commit_hash,
                    "worktree_path": worktree_path,
                    "actual_head": exc.actual_head,
                }
                report["steps"]["local_dev_restore"] = (
                    _restore_local_dev_after_failed_push(
                        repo_root, local_dev_fast_forward, parent_branch
                    )
                )
                wl_add_comment(
                    work_item_id,
                    f"Dev push refused (push-subject mismatch): {exc}\n"
                    f"Commit: {commit_hash}\nBranch: {branch}\n"
                    f"Worktree: {worktree_path}\n"
                    f"Actual HEAD: {exc.actual_head or 'unresolved'}\n"
                    f"Local dev was restored to origin/dev. The commit stays "
                    f"on its feature branch {branch}.",
                )
                raise RuntimeError(
                    f"git push to dev failed (push-subject mismatch): {exc}"
                ) from exc
            except PushTimeoutError as exc:
                # AC3: recoverable state — commit is safe, branch info retained.
                # Restore local dev off the unpushed commit (AC1) so the next
                # finish/sibling does not fork from a base origin/dev lacks,
                # post a manual-push comment, and re-raise so the outer
                # handler resets status to open.
                report["steps"]["push"] = {
                    "success": False,
                    "reason": "timeout",
                    "hash": commit_hash,
                }
                report["steps"]["local_dev_restore"] = (
                    _restore_local_dev_after_failed_push(
                        repo_root, local_dev_fast_forward, parent_branch
                    )
                )
                wl_add_comment(
                    work_item_id,
                    f"Push to dev timed out after {exc.timeout}s (pre-push hook "
                    f"still running, likely the full test suite). Commit "
                    f"{exc.commit_hash} is local on branch {exc.branch}.\n"
                    f"Local dev was restored to origin/dev (the commit stays "
                    f"on its feature branch).\n"
                    f"Push manually: git push origin {exc.branch}:refs/heads/dev",
                )
                raise RuntimeError(f"git push to dev timed out: {exc}") from exc
            except PushNonFastForwardError as exc:
                # AC1/AC2/AC3: dev advanced concurrently — restore local dev
                # (it must never point at the unpushed commit), then rebase
                # and retry inside the (still-present) worktree instead of
                # losing the completed implementation. The ancestry assertion
                # below re-checks the rebased replacement hash.
                report["steps"]["push"] = {
                    "success": False,
                    "reason": "non_fast_forward",
                    "hash": commit_hash,
                }
                report["steps"]["local_dev_restore"] = (
                    _restore_local_dev_after_failed_push(
                        repo_root, local_dev_fast_forward, parent_branch
                    )
                )
                LOG.info(
                    "Push failed (non-fast-forward); attempting rebase and retry "
                    "...",
                )
                ff_retry: dict[str, Any] = {}
                commit_hash = _recover_non_fast_forward_push(
                    worktree_path=worktree_path,
                    repo_root=repo_root,
                    branch=branch,
                    commit_hash=commit_hash,
                    exc=exc,
                    work_item_id=work_item_id,
                    evidence_out=push_evidence,
                    parent_branch=parent_branch,
                    ff_retry_out=ff_retry,
                )
                # AC1 (SA-0MW3H07A8004Q54C): record the post-rebase
                # fast-forward retry, and treat its own churn as finish's
                # (never as a foreign mutation).
                if ff_retry:
                    local_dev_fast_forward["retry"] = ff_retry
                    report["steps"]["local_dev_fast_forward"] = (
                        local_dev_fast_forward
                    )
                    main_checkout_guard["own_churn"] = list(
                        main_checkout_guard.get("own_churn") or []
                    ) + list(ff_retry.get("own_churn") or [])
                # The rebase rewrote HEAD; keep the reported commit in sync.
                report["steps"]["commit"]["hash"] = commit_hash
                report["steps"]["push"] = {
                    "success": True,
                    "recovered": "non_fast_forward",
                    "hash": commit_hash,
                    "gate_evidence": push_evidence,
                }
                # An interruption from here on names the rebased replacement
                # hash, which is the commit that actually landed.
                _set_finish_progress(commit=commit_hash, pushed=True)
            except PushGateFailureError as exc:
                # AC2: the pre-push gate said *why* the push failed. Report the
                # machine-readable reason in the finish report and post it on
                # the work item with the matching remediation, instead of the
                # generic "git push to dev failed".
                push_step: dict[str, Any] = {
                    "success": False,
                    "reason": exc.gate_reason,
                    "hash": commit_hash,
                    "gate_stderr_tail": exc.stderr_tail,
                }
                guard_details = push_evidence.get("pushed_commit_guard")
                if guard_details:
                    push_step["refused_by_pushed_commit_guard"] = guard_details
                report["steps"]["push"] = push_step
                report["steps"]["local_dev_restore"] = (
                    _restore_local_dev_after_failed_push(
                        repo_root, local_dev_fast_forward, parent_branch
                    )
                )
                wl_add_comment(
                    work_item_id,
                    _dev_push_gate_failure_comment(
                        exc.gate_reason,
                        exc.stderr_tail,
                        commit_hash,
                        branch,
                        worktree_path,
                    ),
                )
                raise RuntimeError(
                    f"git push to dev failed (gate reason: {exc.gate_reason}). "
                    f"{_push_gate_remediation(exc.gate_reason)}"
                ) from exc
            except RuntimeError:
                # A generic push failure: nothing reached origin. Undo our own
                # fast-forward so local dev never stays on the unpushed commit
                # (the exact orphan that made the next finish fork from a base
                # origin/dev did not have).
                report["steps"]["push"] = {
                    "success": False,
                    "reason": "push_failed",
                    "hash": commit_hash,
                }
                report["steps"]["local_dev_restore"] = (
                    _restore_local_dev_after_failed_push(
                        repo_root, local_dev_fast_forward, parent_branch
                    )
                )
                raise
            else:
                report["steps"]["push"] = {
                    "success": True,
                    "hash": commit_hash,
                    "gate_evidence": push_evidence,
                }
                # The commit is now on dev: the interruption record must say
                # so (AC2), so a released item is never mistaken for
                # unpublished work.
                _set_finish_progress(commit=commit_hash, pushed=True)
            LOG.info("Push to dev succeeded")

            # ── Step 6a: Post-push ancestry assertion (AC2) ───────────────
            # The push's return value is not trusted on its own: finish may
            # only report success when its own commit (including a rebased
            # replacement hash) is an ancestor of the pushed origin/dev tip
            # (SA-0MVPZG7A0000N66U AC2). The assertion fails closed — raising
            # `AncestryAssertionError` here keeps the worktree and the feature
            # branch, leaves the work item un-advanced, and is reported by the
            # outer handler with the commit hash.
            report["steps"]["ancestry_assertion"] = _assert_commit_on_origin_dev(
                repo_root,
                commit_hash,
                parent_branch=parent_branch,
                branch=branch,
                worktree_path=worktree_path,
            )

            # ── Step 6a.5: Post-push full-suite verification (read-only) ──
            # Design A (SA-0MVXUSED6008J04R): after a successful push the run
            # resolves the full-suite verdict for the pushed tree from the
            # test cache — the dev-push pre-push hook already ran it against
            # exactly this commit. Nothing is executed here. A ``red`` or
            # ``missing`` verdict **refuses the terminal transition here**,
            # before any terminal state is observable, so the item is never
            # briefly audit-dispatchable on a run that cannot prove green
            # (SA-0MVXUSJ2P009T97H AC4). The pushed commit is never reverted
            # or force-removed: the refusal releases the item to ``open`` and
            # names the commit, the retained branch/worktree and the
            # remediation.
            #
            # Ordering invariant (SA-0MVXUSK02003ZMDN): this verification is a
            # *finalisation* step, not a post-``in_review`` session step — it
            # runs inside the held, non-terminal ``StatusLifecycle`` window so
            # the item is never an audit-dispatch candidate while it runs.
            post_push_verification = _resolve_post_push_verification(
                worktree_path, repo_root
            )
            report["steps"]["post_push_verification"] = post_push_verification
            if post_push_verification["verdict"] not in (
                POST_PUSH_VERDICT_GREEN,
                POST_PUSH_VERDICT_NOT_APPLICABLE,
            ):
                refusal_message, refusal_comment = (
                    _post_push_verification_refusal(
                        post_push_verification,
                        work_item_id,
                        commit_hash,
                        branch,
                        worktree_path,
                    )
                )
                # AC4: the refusal is routed through the triage helper for
                # BOTH unprovable verdicts — a red entry files the real
                # failing test, a missing entry files the verification gap
                # itself — so the failure is never only a prose comment.
                post_push_verification["triage"] = (
                    _route_post_push_failure_to_triage(
                        post_push_verification, commit_hash, work_item_id
                    )
                )
                report["hash"] = commit_hash
                wl_add_comment(work_item_id, refusal_comment)
                raise RuntimeError(refusal_message)

            # ── Step 6b: Worktree removal ownership ───────────────────────
            # One owner per worktree (SA-0MVTE8XAJ002IUDR): a driven child
            # session's worktree belongs to the driver (removed once that
            # session has exited), and ``--keep-worktree`` /
            # ``IMPLEMENT_KEEP_WORKTREE=1`` hands ownership to the caller.
            # Removing it here would delete the finishing session's own cwd
            # and leave it unable to run any later command.
            #
            # Ordering invariant (SA-0MVXUSK02003ZMDN): a *deferred* removal
            # is not a remaining lifecycle step — ownership has passed to the
            # driver/caller and it can no longer change the work item. Only
            # the non-deferred removal runs here, before the terminal write.
            defer_removal, defer_reason = _should_defer_worktree_removal(
                keep_worktree
            )
            report["steps"]["worktree_removal"] = {
                "deferred": defer_removal,
                "reason": defer_reason,
                "worktree_path": worktree_path,
            }
            # The follow-up surface for a retained tree (SA-0MVTHJ3PJ000IVSK
            # AC2): the report names the path, the reason and the exact
            # reviewed cleanup action. Empty on the default (non-deferred)
            # path, where finish has already removed the tree itself.
            report["steps"]["worktree_cleanup"] = {
                "deferred": defer_removal,
                "path": worktree_path,
                "reason": defer_reason,
                "command": (
                    _worktree_cleanup_command(work_item_id)
                    if defer_removal
                    else ""
                ),
            }

            if defer_removal:
                # Keep the tree, its branch and .implement_state.json: the
                # session keeps working and a resume can re-enter it. The
                # deferral is never silent — the report, a log line and the
                # completion comment all name the retained path.
                report["steps"]["worktree_removed"] = False
                LOG.info(
                    "Worktree removal deferred (%s); retained at %s.\n"
                    "  The driver removes it for a driven child session; "
                    "otherwise remove it yourself once this session is "
                    "finished with it:\n"
                    "  git worktree remove %s && git worktree prune",
                    defer_reason,
                    worktree_path,
                    worktree_path,
                )
            else:
                # ── Step 6b: Clean up worktree processes ──────────────────
                # Runs *after* the push (historically it ran before, at Step
                # 5b): a cleanup that misbehaves can no longer pre-empt the
                # push, which is the failure mode that stranded commits
                # local-only (SA-0MVJC5KNT0044L2B). The cleanup still
                # precedes the worktree removal so stray sessions holding cwd
                # in the tree do not block ``git worktree remove``.
                # cleanup_worktree_processes itself is additionally
                # self-exclusion-safe (see its docstring).
                LOG.info("Cleaning up worktree processes...")
                cleanup_result = cleanup_worktree_processes(worktree_path)
                report["steps"]["cleanup"] = cleanup_result
                if cleanup_result.get("warning"):
                    LOG.warning(
                        "Process cleanup warning: %s", cleanup_result["warning"]
                    )

            # ── Step 6c: Main-checkout guard verify (pre-removal) ────────
            # Re-verify once more before tearing the worktree down: a
            # mutation that appeared during the push (e.g. the pre-push
            # hook) must still abort loudly instead of being left silently
            # in place. Runs on the deferred path too — the guard is about
            # the main checkout, not the worktree.
            _abort_on_main_checkout_violation(
                "pre_worktree_removal", main_checkout_guard["own_churn"]
            )

            # ── Step 7: Remove worktree (unless deferred) ─────────────────
            # Only after a successful push — a failed push keeps the
            # worktree so the recovery rebase and manual recovery remain
            # possible (WL-0MV533YSW004D1E1).
            if not defer_removal:
                LOG.info("Removing worktree...")
                remove_state(worktree_path)
                try:
                    removed = bool(
                        _remove_worktree(
                            worktree_path,
                            repo_root=repo_root,
                        )
                    )
                except RuntimeError as exc:
                    # A confinement refusal (the path is not a worktree this
                    # repository registers) must never turn a successful,
                    # already-pushed finish into a failure: the tree is
                    # retained and reported instead (SA-0MVXVC0KB004XDH0).
                    removed = False
                    report["steps"]["worktree_removal"]["refused"] = str(exc)
                    LOG.warning("Worktree removal refused: %s", exc)
                report["steps"]["worktree_removed"] = removed
                if not removed:
                    LOG.warning(
                        "Failed to remove worktree at %s", worktree_path
                    )

            # ── Step 8: Sync local parent branch (AC1 / AC2) ──────────────
            # After a successful push, bring the main checkout's local
            # parent branch (dev) up to date so the next child worktree
            # does not start from a stale base.
            sync_parent_branch = parent_branch
            LOG.info("Syncing local parent branch %s...", sync_parent_branch)
            sync_result = _sync_parent_branch(repo_root, sync_parent_branch)
            report["steps"]["sync_parent_branch"] = sync_result
            if sync_result.get("warning"):
                LOG.info("Parent-branch sync: %s", sync_result["warning"])

            # ── Step 9: Completion comment (BEFORE the terminal write) ────
            # Design A (SA-0MVXUSED6008J04R AC3): the comment carries the
            # pushed commit hash and runs inside the held, non-terminal
            # window, so no comment follows the terminal write — which also
            # preserves the audit-freshness ordering rule
            # (SA-0MTHC710X003ORZM: a post-audit comment can invalidate a
            # passed audit's freshness window). Verdict provenance is included
            # so the accepted green is attributable from the work item alone
            # (SA-0MVQHVJEX0071R7G).
            #
            # Ordering invariant (SA-0MVXUSK02003ZMDN): the completion comment
            # is the last store write *before* the terminal transition; posting
            # it after the terminal write would bump ``updatedAt`` on an
            # already audit-ready item (and race the dispatcher).
            verdict_lines = "\n".join(_verdict_provenance_lines(report))
            completion_comment = (
                f"Implementation complete.\n- Commit: {commit_hash}\n"
                f"- Branch: {branch}\n- Worktree: {worktree_path}\n"
                f"{verdict_lines}"
            )
            verification_step = report["steps"].get(
                "post_push_verification") or {}
            verification_verdict = verification_step.get("verdict")
            if verification_verdict:
                # Never a silent green: name the read-only post-push verdict
                # (and why, when it is not applicable) in the comment.
                completion_comment += (
                    f"\n- Post-push verification: {verification_verdict}"
                )
                if verification_step.get("reason"):
                    completion_comment += (
                        f" ({verification_step['reason']})"
                    )
            removal_step = report["steps"].get("worktree_removal") or {}
            if removal_step.get("deferred"):
                # Never let a deferred cleanup disappear: name the retained
                # tree, the reason and the exact remedy on the work item
                # itself (SA-0MVTE8XAJ002IUDR AC2). Ownership of this cleanup
                # has passed to the driver/caller, so it is not a remaining
                # lifecycle step of this run (SA-0MVXUSJ2P009T97H AC1).
                completion_comment += (
                    f"\n- Worktree retained (deferred removal: "
                    f"{removal_step.get('reason', 'unknown')}).\n"
                    f"  A driven child session's worktree is removed by "
                    f"`implement.py drive` once the session exits. Otherwise "
                    f"remove it as the LAST command, once this session no "
                    f"longer needs its cwd:\n"
                    f"  {_worktree_cleanup_command(work_item_id)}\n"
                    f"  manual fallback: git worktree remove {worktree_path} "
                    f"&& git worktree prune"
                )
            # AC4 (SA-0MVXUSJJY009HMQ0): a re-run after an interruption
            # converges without duplicating this record. A prior run may have
            # posted it before its terminal write was lost; the stable marker
            # plus the commit hash identifies that record, so the re-run only
            # completes the lifecycle instead of stacking a second completion
            # comment.
            completion_already_recorded = _comment_already_recorded(
                work_item_id,
                COMPLETION_COMMENT_MARKER,
                f"Commit: {commit_hash}",
            )
            report["steps"]["completion_comment"] = {
                "posted": not completion_already_recorded,
                "deduplicated": completion_already_recorded,
                "commit": commit_hash,
            }
            if completion_already_recorded:
                LOG.info(
                    "Completion comment for %s (commit %s) is already "
                    "recorded; skipping the duplicate on the re-run.",
                    work_item_id,
                    commit_hash,
                )
            else:
                wl_add_comment(work_item_id, completion_comment)

            # StatusLifecycle.__exit__ sets status=completed, stage=in_review
            # — the run's LAST lifecycle action (assignee released with it).
            # Ordering invariant (SA-0MVXUSK02003ZMDN): the terminal write and
            # the assignee release are inseparable and last; every step that
            # can still change the work item (the read-only post-push
            # verification, the completion comment, the non-deferred worktree
            # removal) has already run above, so the item becomes an
            # audit-dispatch candidate only when the run is fully complete.

        # ── Step 8b: Terminal-status read-back assertion ──────────────
        # (OSL-0MVME1PI5005WU58) The terminal write above must have stuck
        # in the main-checkout store on BOTH the push-with-diff and the
        # no-op-push path; a finish that reports success while the store is
        # still non-terminal is a silent lifecycle lie (recorded on
        # OSL-0MVMB44C9008IXZX). Runs only after the confirmed successful
        # push (Step 6) — a genuine push failure can never reach here.
        #
        # Ordering invariant (SA-0MVXUSK02003ZMDN): this read-only read-back
        # is the phase's last action. It observes the terminal state but never
        # changes it, so it cannot reopen the audit-dispatch window the
        # terminal write just closed.
        report["steps"]["status_readback"] = _assert_terminal_status(
            work_item_id
        )

    except RuntimeError as e:
        msg = str(e)
        report["success"] = False
        report["message"] = msg
        # AC5 (LP-0MVWEP1MM0097RAT): every finish failure exit records a
        # structured steps entry so no classification path depends on
        # ``report["message"]`` prose. Specific steps (push, commit, ancestry,
        # guard) already record their own entries where they are raised; a
        # generic runtime error that escapes without one is recorded under
        # ``finish`` and classifies as ``unknown`` (fail-safe — never
        # ``infrastructure``).
        report["steps"]["finish"] = {
            "success": False,
            "reason": "runtime_error",
            "exception": type(e).__name__,
        }
        # Status was restored to original by StatusLifecycle; override to open
        try:
            StatusLifecycle.update_status(work_item_id, "open", assignee="")
        except RuntimeError:
            LOG.error("Failed to reset work item %s status to open", work_item_id)
        if "git push to dev failed" in msg or "git push to dev timed out" in msg:
            # Already posted a manual-push comment from the push handler;
            # the commit hash and branch are in the comment already.
            LOG.warning(
                "Push %s. Commit %s on branch %s is local and can be "
                "pushed manually.",
                "timed out" if "timed out" in msg else "failed",
                commit_hash,
                branch,
            )
        elif "git commit failed" in msg:
            wl_add_comment(work_item_id, "Commit failed during finish phase.")
        elif isinstance(e, AncestryAssertionError):
            # AC2: the push claimed success but the commit never reached
            # origin/dev. Record the refusal (and the retained recovery
            # state) on the work item; the status reset above keeps it open.
            # AC5 (LP-0MVWEP1MM0097RAT): the exit that previously carried no
            # structured steps entry now records one under
            # ``ancestry_assertion`` (classifies as ``infrastructure``,
            # matching the parent epic's allow-list).
            report["steps"]["ancestry_assertion"] = {
                "success": False,
                "reason": "ancestry_failed",
                "commit": e.resolved_commit or e.commit_hash,
                "target_ref": e.target_ref,
                "target_tip": e.target_tip,
            }
            wl_add_comment(
                work_item_id,
                "Finish refused: ancestry assertion failed.\n"
                f"- Commit: {e.resolved_commit or e.commit_hash}\n"
                f"- Target: {e.target_ref} (tip {e.target_tip or 'unresolved'})\n"
                f"- Reason: {e.reason}\n"
                f"- Branch: {branch} (retained)\n"
                f"- Worktree: {worktree_path} (retained)\n"
                "The work item stays open; re-run finish once the commit is "
                "pushed (rebase onto the remote branch if the push is "
                "rejected).",
            )
        # AC2 (SA-0MVXUSJJY009HMQ0): once a commit exists, every failure exit
        # records the retained state — status open, assignee cleared, the
        # commit (published or local) and the resume command — so an
        # interrupted run is never released without an explanation. It runs
        # after the specific comments above and deduplicates on the marker
        # plus commit, so it is recorded once per commit however many times
        # finish is retried.
        if (
            _finish_progress_global.get("work_item_id") == work_item_id
            and _finish_progress_global.get("commit")
        ):
            try:
                report["steps"]["interruption_comment"] = (
                    _record_interruption_comment(
                        work_item_id,
                        f"finish failed ({msg.splitlines()[0][:200]})",
                        worktree_retained=bool(
                            worktree_path and Path(worktree_path).exists()
                        ),
                        worktree_path=worktree_path,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - never mask the failure
                LOG.error(
                    "Failed to record the interruption on %s: %s",
                    work_item_id, exc,
                )
        if json_output:
            print(format_json_output(report))
        else:
            LOG.error(msg)
        return report
    except Exception as exc:
        # An *unexpected* failure (not one of the classified RuntimeErrors
        # above) already had its original status restored by
        # ``StatusLifecycle.__exit__`` — but for a finishing item that
        # original status is ``in-progress``, so the claim would survive the
        # crash on its scheduling hold. Release it durably before reporting
        # (SA-0MVVF75ZA007XNKB). The conditional reset never clobbers an item
        # that already reached a terminal state.
        report["unexpected_error"] = f"{type(exc).__name__}: {exc}"
        report["success"] = False
        report["message"] = f"Unexpected error during finish: {exc}"
        LOG.exception(
            "Unexpected error during finish of %s", work_item_id,
        )
        try:
            _safety_reset_if_in_progress(work_item_id)
        except RuntimeError:
            LOG.error(
                "Failed to release the claim on %s after an unexpected "
                "error",
                work_item_id,
            )
        # AC2 (SA-0MVXUSJJY009HMQ0): the same retained-state record as the
        # classified path — an unexpected crash after the commit/push must
        # still name what survived and how to resume.
        if (
            _finish_progress_global.get("work_item_id") == work_item_id
            and _finish_progress_global.get("commit")
        ):
            try:
                report["steps"]["interruption_comment"] = (
                    _record_interruption_comment(
                        work_item_id,
                        f"unexpected error ({type(exc).__name__})",
                        worktree_retained=bool(
                            worktree_path and Path(worktree_path).exists()
                        ),
                        worktree_path=worktree_path,
                    )
                )
            except Exception as record_exc:  # noqa: BLE001 - never mask it
                LOG.error(
                    "Failed to record the interruption on %s: %s",
                    work_item_id, record_exc,
                )
        if json_output:
            print(format_json_output(report))
        return report
    finally:
        # Released on EVERY exit path from the critical section (success,
        # RuntimeError, or an escaping exception): the kernel also releases
        # the flock if this process dies.
        _release_main_checkout_lock(lock_fd)
        # The interruption context only ever describes a half-finished run:
        # clear it so a later signal cannot misreport a converged phase
        # (SA-0MVXUSJJY009HMQ0 AC2).
        _clear_finish_progress()

    # ── Step 10: Success reporting ───────────────────────────────
    # The completion comment already ran inside the lifecycle window
    # (Step 9, before the terminal write) — nothing lifecycle-relevant is
    # left here, so the item is already audit-ready.
    removal_step = report["steps"].get("worktree_removal") or {}

    report["success"] = True
    report["hash"] = commit_hash
    report["message"] = f"Implementation complete. Commit {commit_hash} pushed to dev."

    if json_output:
        print(format_json_output(report))
    else:
        print()
        print("=" * 60)
        print(f"  ✅ Implementation complete for {work_item_id}")
        print("=" * 60)
        print(f"  Commit: {commit_hash}")
        print(f"  Branch: {branch}")
        print("  Status: in_review")
        if removal_step.get("deferred"):
            # A retained tree is an outstanding action, never a silent one
            # (SA-0MVTHJ3PJ000IVSK AC2): name the path, the reason and the
            # exact command that finishes the job.
            print(
                "  Worktree retained: "
                f"{removal_step.get('worktree_path', worktree_path)}"
            )
            print(
                "  Deferred removal: "
                f"{removal_step.get('reason', 'unknown')}"
            )
            print(
                "  Remove it as your LAST command: "
                f"{_worktree_cleanup_command(work_item_id)}"
            )
        print()

    # Surface the project-local prose hook after the final step.
    _surface_postfix_prose(repo_root, json_output, report)

    return report


def _mark_pane_aborted() -> None:
    """Set the herdr pane title to the red state on abort (fail-open).

    Ensures a failed/aborted implementation surfaces in the pane grid even
    when no end-of-session report is rendered.
    """
    try:
        from shared.herdr_pane import mark_aborted

        mark_aborted()
    except Exception:  # pane updates must never break abort
        LOG.debug("herdr pane abort status update failed", exc_info=True)


def phase_abort(
    work_item_id: str,
    json_output: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """Abort the implementation workflow.

    Steps:
    1. Reset work item status to open
    2. Find and cleanup worktree
    3. Remove worktree
    4. Log abort comment

    Args:
        work_item_id: The work item ID.
        json_output: If True, output JSON.
        verbose: Enable verbose logging.

    Returns:
        Dict with result information.
    """
    report: dict[str, Any] = {
        "phase": "abort",
        "work_item_id": work_item_id,
        "success": True,
        "message": "",
    }

    # ── Step 1: Reset status ───────────────────────────────────────
    LOG.info("Resetting work item %s status to open...", work_item_id)
    try:
        StatusLifecycle.update_status(work_item_id, "open", assignee="")
    except RuntimeError:
        LOG.error("Failed to reset work item %s status to open", work_item_id)

    # Surface the abort in the herdr pane grid (fail-open; AC4).
    _mark_pane_aborted()

    # ── Step 2: Find and cleanup worktree ──────────────────────────
    worktree_path = _discover_worktree(work_item_id)
    # Resolve the MAIN checkout root *before* the worktree is torn down: the
    # stale-state rejection needs the worktree to exist, and aborting from
    # inside the worktree must restore the main checkout, never the worktree
    # (MS-0MVM7NJF70037YU4).
    repo_root = _resolve_main_repo_root(
        worktree_path, read_state(worktree_path) if worktree_path else None
    )
    if worktree_path and Path(worktree_path).exists():
        LOG.info("Cleaning up worktree at %s...", worktree_path)
        cleanup_worktree_processes(worktree_path)
        remove_state(worktree_path)
        try:
            _remove_worktree(worktree_path)
        except RuntimeError as exc:
            # A confinement refusal retains the tree; an abort never fails
            # because of it (SA-0MVXVC0KB004XDH0).
            report["worktree_refused"] = str(exc)
            LOG.warning("Worktree removal refused during abort: %s", exc)
        report["worktree_path"] = worktree_path

    # ── Step 3: Restore repo state ─────────────────────────────────
    _restore_repo_state(repo_root)

    # ── Step 4: Log comment ────────────────────────────────────────
    wl_add_comment(work_item_id, "Implementation aborted.")

    report["message"] = f"Work item {work_item_id} aborted and cleaned up."

    if json_output:
        print(format_json_output(report))
    else:
        print(f"\n✅ Implementation aborted for {work_item_id}. Worktree cleaned up.\n")

    return report


#: Statuses that permit :func:`phase_cleanup` to remove a worktree.
#:
#: Deliberately narrower than :data:`TERMINAL_STATUSES`: the post-finish
#: cleanup contract is "the item has finished" (``completed`` /
#: ``in_review``), not "the item is terminal for parent advancement" (which
#: also covers ``done`` and soft-``deleted`` items whose trees nobody asked
#: this action to remove).
CLEANUP_REMOVABLE_STATUSES = frozenset({"completed", "in_review"})


def _resolve_cleanup_worktree(
    work_item_id: str, worktree_path: str | None = None
) -> str | None:
    """Resolve the worktree :func:`phase_cleanup` should act on.

    Resolution order (the work item's contract):

    1. **``.implement_state.json``** — the state file next to the candidate
       path (``--worktree-path``) or in the current directory, provided it
       records *this* work item. The state file is the authoritative record
       of which tree belongs to which item, so it wins over a bare override.
    2. **``--worktree-path``** — the operator's explicit override.
    3. **:func:`_discover_worktree`** — the usual scan of
       ``.worklog/worktrees/wl-<id>-*``.

    Args:
        work_item_id: The work item that owns the tree.
        worktree_path: The ``--worktree-path`` override, if any.

    Returns:
        Absolute worktree path, or None when none can be resolved.
    """
    candidates: list[str] = []
    if worktree_path:
        candidates.append(str(Path(worktree_path).expanduser().resolve()))
    try:
        cwd = str(Path.cwd().resolve())
    except OSError:  # pragma: no cover - cwd already gone
        cwd = ""
    if cwd and cwd not in candidates:
        candidates.append(cwd)

    for candidate in candidates:
        if not (Path(candidate) / STATE_FILE_NAME).exists():
            continue
        # A state file records which item owns the tree: a candidate whose
        # state belongs to a *different* item is not this item's worktree.
        candidate_state = read_state(candidate)
        if candidate_state is None or candidate_state.work_item_id == work_item_id:
            return candidate
    if worktree_path:
        # No state file anywhere: the explicit override is the best
        # information available (an already-absent tree still needs clearing).
        return str(Path(worktree_path).expanduser().resolve())
    return _discover_worktree(work_item_id)


def phase_cleanup(
    work_item_id: str,
    json_output: bool = False,
    verbose: bool = False,
    worktree_path: str | None = None,
) -> dict[str, Any]:
    """Remove a finished item's worktree — the session's final bash command.

    **Ordering contract: run this as the session's LAST bash command.**
    Removal deletes the tree the invoking session stands in (the plain leaf
    flow's cwd *is* the worktree), so nothing that needs the worktree cwd may
    follow it: every later command would fail with a deleted cwd. Render the
    end-of-session report first, then call this action last. The completion
    comment and the read-only post-push verification are **not** session
    steps: they already ran inside ``finish``, before the terminal write
    (SA-0MVXUSK02003ZMDN).

    Gates, in order — a failure at any of them **retains** the tree and is
    reported (never forced past, never silent):

    1. resolve the worktree (:func:`_resolve_cleanup_worktree`); none found →
       ``retained_no_worktree``,
    2. the work item must be ``completed`` / ``in_review``
       (:data:`CLEANUP_REMOVABLE_STATUSES`) → otherwise
       ``retained_non_terminal``,
    3. the resolved path must not be the main checkout → otherwise
       ``retained_main_checkout``,
    4. an existing path must actually be a git worktree (``.git`` is a
       *file*), so an arbitrary directory is never handed to a removal
       mechanism → otherwise ``retained_no_worktree``,
    5. the tree must hold no uncommitted work → otherwise
       ``retained_dirty``.

    Removal itself is :func:`_remove_finished_worktree` — the single removal
    path shared with the driver (``cleanup_worktree_processes`` →
    ``_remove_worktree`` → ``remove_state``), so this action adds **no** new
    deletion mechanism and never passes ``--force`` past a gate.

    Idempotent by construction: an already-absent tree is a reported no-op
    success (``already_absent``) that still clears a stale state file, so an
    interrupted workflow can simply be re-run.

    Args:
        work_item_id: The work item that owns the worktree.
        json_output: Emit JSON instead of human-readable lines.
        verbose: Unused; kept for CLI symmetry.
        worktree_path: Optional ``--worktree-path`` override.

    Returns:
        A report dict with ``removed``, ``path``, ``already_absent``,
        ``retained_dirty``, ``retained_non_terminal``,
        ``retained_no_worktree``, ``retained_main_checkout``,
        ``removed_state``, ``reason``, ``remedy`` and ``message``. Retained is
        a reported outcome (``success`` stays true); only an unexpected
        removal failure reports ``success: false``. ``removed_state`` means
        "no ``.implement_state.json`` remains at ``path``" — true for a
        successful removal *and* for an already-absent tree (the stale state
        file, if any, is cleared either way).
    """
    report: dict[str, Any] = {
        "phase": "cleanup",
        "work_item_id": work_item_id,
        "success": True,
        "path": "",
        "removed": False,
        "already_absent": False,
        "retained_dirty": False,
        "retained_non_terminal": False,
        "retained_no_worktree": False,
        "retained_main_checkout": False,
        "retained_other_item": False,
        "removed_state": False,
        "reason": "",
        "remedy": "",
        "message": "",
    }

    # ── Step 1: Resolve the worktree ───────────────────────────────
    path = _resolve_cleanup_worktree(work_item_id, worktree_path)
    report["path"] = path or ""
    if not path:
        report["retained_no_worktree"] = True
        report["message"] = (
            f"No worktree found for {work_item_id}; nothing to clean up."
        )
        print(
            format_json_output(report)
            if json_output
            else f"\nℹ️  No worktree found for {work_item_id}; nothing to clean up.\n"
        )
        return report

    # ── Step 2: Terminal gate ──────────────────────────────────────
    item = wl_show(work_item_id)
    status = str((item or {}).get("status", "") or "")
    if status not in CLEANUP_REMOVABLE_STATUSES:
        report["retained_non_terminal"] = True
        report["reason"] = f"work item status is '{status or 'unknown'}'"
        report["message"] = (
            f"Worktree at {path} retained: work item {work_item_id} is "
            f"'{status or 'unknown'}', not completed/in_review.\n"
            f"  Re-run once the item is finished: "
            f"python3 skill/implement/scripts/implement.py cleanup "
            f"{work_item_id}"
        )
        print(
            format_json_output(report)
            if json_output
            else f"\n⚠️  {report['message']}\n"
        )
        return report

    # ── Step 2b: The tree must belong to this work item ────────────
    # A state file written for a different item (e.g. the session's own
    # worktree while ``--worktree-path`` names another) must never authorise
    # deleting someone else's work.
    state = read_state(path) if Path(path).exists() else None
    if state is not None and state.work_item_id and state.work_item_id != work_item_id:
        report["retained_other_item"] = True
        report["reason"] = f"worktree belongs to {state.work_item_id}"
        report["remedy"] = (
            f"run cleanup for {state.work_item_id}, or pass "
            f"--worktree-path pointing at {work_item_id}'s own worktree"
        )
        report["message"] = (
            f"Refusing to remove {path}: its {STATE_FILE_NAME} belongs to "
            f"{state.work_item_id}, not {work_item_id}.\n  {report['remedy']}"
        )
        print(
            format_json_output(report)
            if json_output
            else f"\n⚠️  {report['message']}\n"
        )
        return report

    # ── Step 3: Never treat the main checkout as a worktree ────────
    repo_root = _resolve_main_repo_root(path, state)
    report["repo_root"] = repo_root
    if repo_root and Path(path).resolve() == Path(repo_root).resolve():
        report["retained_main_checkout"] = True
        report["reason"] = "resolved path is the main checkout"
        report["remedy"] = (
            "inspect .implement_state.json — a state file in the repository "
            "root is a stale artifact, not a worktree"
        )
        report["message"] = (
            f"Refusing to remove {path}: it resolves to the main checkout "
            f"({repo_root}), not a worktree.\n  {report['remedy']}"
        )
        print(
            format_json_output(report)
            if json_output
            else f"\n⚠️  {report['message']}\n"
        )
        return report

    # ── Step 3b: An existing path must really be a worktree ────────
    # ``_remove_worktree`` falls back to a manual ``shutil.rmtree`` when
    # ``git worktree remove`` fails and the target has no ``.git/``
    # directory, so an arbitrary directory (or a tree whose registration was
    # already pruned) must never reach it: ``.git`` is a *file* in a linked
    # worktree and a *directory* in the main checkout.
    if Path(path).is_dir() and not _is_worktree(Path(path)):
        report["retained_no_worktree"] = True
        report["reason"] = "path is not a git worktree"
        report["remedy"] = (
            f"inspect {path} (ls -la {path}/.git) — only a linked worktree "
            f"may be removed automatically"
        )
        report["message"] = (
            f"Refusing to remove {path}: it is not a git worktree "
            f"(.git is a directory or absent).\n  {report['remedy']}"
        )
        print(
            format_json_output(report)
            if json_output
            else f"\n⚠️  {report['message']}\n"
        )
        return report

    # ── Step 4: Remove behind the shared gates ─────────────────────
    LOG.info("Cleaning up worktree %s for %s...", path, work_item_id)
    outcome = _remove_finished_worktree(work_item_id, path, repo_root)
    report["removed"] = bool(outcome.get("removed"))
    report["already_absent"] = bool(outcome.get("already_absent"))
    report["retained_dirty"] = bool(outcome.get("retained_dirty"))
    # "No state file remains at the path" — true after a removal and for an
    # already-absent tree (``_remove_finished_worktree`` clears the stale file
    # in both cases, so a repeat run can never resume a dead tree).
    report["removed_state"] = not (Path(path) / STATE_FILE_NAME).exists()
    report["reason"] = str(outcome.get("reason", ""))
    if outcome.get("remedy"):
        report["remedy"] = str(outcome["remedy"])

    if report["removed"]:
        report["message"] = f"Removed worktree at {path}."
    elif report["already_absent"]:
        report["message"] = (
            f"Worktree at {path} was already absent; nothing to do."
        )
    elif report["retained_dirty"]:
        # Actionable remedy: the exact inspection and removal commands.
        report["remedy"] = (
            f"git -C {path} status  # commit or discard the changes\n"
            f"  git worktree remove {path} && git worktree prune"
        )
        report["message"] = (
            f"Worktree at {path} retained: it has uncommitted changes "
            f"(never discarded).\n  {report['remedy']}"
        )
    else:
        if not outcome.get("refused"):
            # ``refused`` is already a reported (safe) outcome; anything else
            # that removed nothing is an unexpected failure.
            report["success"] = False
        report["remedy"] = report["remedy"] or (
            f"git worktree remove {path} && git worktree prune"
        )
        report["message"] = (
            f"Worktree at {path} was not removed ({report['reason'] or 'unknown'}).\n"
            f"  {report['remedy']}"
        )

    if json_output:
        print(format_json_output(report))
    else:
        print(
            f"\n{'✅' if report['removed'] else 'ℹ️ '} {report['message']}\n"
        )
    return report


def _build_implement_cmd(
    action: str,
    work_item_id: str,
    extra_flags: list[str] | None = None,
) -> list[str]:
    """Build a subprocess command to invoke implement.py.

    Args:
        action: One of 'start', 'finish', 'abort', 'parent'.
        work_item_id: The work item ID.
        extra_flags: Additional flags (e.g. '--json', '--no-refactor').

    Returns:
        A command list suitable for ``subprocess.run``.
    """
    script_path = str(Path(__file__).resolve())
    cmd = [sys.executable, script_path, action, work_item_id, "--json"]
    if extra_flags:
        cmd.extend(extra_flags)
    return cmd


def _invoke_implement(
    action: str,
    work_item_id: str,
    no_refactor: bool = False,
    verbose: bool = False,
    parse_on_failure: bool = False,
) -> dict[str, Any] | None:
    """Run an implement phase as a subprocess and parse JSON output.

    Args:
        action: One of 'start', 'finish', 'abort', 'parent'.
        work_item_id: The work item ID.
        no_refactor: If True, add ``--no-refactor`` flag.
        verbose: If True, add ``-v`` flag.
        parse_on_failure: If True, return the parsed JSON report even when the
            subprocess exits non-zero (so an actionable failure — e.g. the
            worktree-placement guard — is surfaced to the caller instead of a
            generic error). Callers that gate on the ``None`` sentinel (start,
            finish) must leave this ``False``.

    Returns:
        Parsed JSON result dict on success (exit code 0), or ``None``
        if the subprocess failed (exit code non-zero). When
        *parse_on_failure* is set, a parseable non-zero-exit report is returned
        instead of ``None``.
    """
    flags: list[str] = []
    if no_refactor:
        flags.append("--no-refactor")
    if verbose:
        flags.append("-v")
    cmd = _build_implement_cmd(action, work_item_id, extra_flags=flags)
    LOG.debug("Subprocess: %s", " ".join(cmd))
    repo_root = _get_repo_root() or str(Path.cwd())
    # Propagate the holder chain's ownership token explicitly (AC4) so a
    # subprocess that runs ``phase_start`` for the held child re-enters the
    # lock instead of being falsely refused: the driver's own ``parent`` →
    # ``start`` chain must never block on its own lock.
    env = dict(os.environ)
    token = _child_lock_token(env)
    if token:
        env[ENV_LOCK_TOKEN] = token
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_resolve_test_timeout(repo_root),
            env=env,
        )
    except _SUBPROCESS_TIMEOUT_EXPIRED:
        # A finish that overruns its bound is killed (``subprocess.run``
        # SIGKILLs the child before raising), so the killed phase never runs
        # its own release. Report the failure as ``None`` so the caller's
        # failure path releases the orphaned claim instead of dying with a
        # traceback (SA-0MVVF75ZA007XNKB).
        LOG.error(
            "Subprocess %s %s exceeded its timeout bound and was killed",
            action, work_item_id,
        )
        return None
    if result.returncode != 0:
        LOG.error(
            "Subprocess failed (rc=%d): %s\nSTDERR: %s",
            result.returncode,
            result.stdout[:500],
            result.stderr[:500],
        )
        if parse_on_failure:
            try:
                parsed = json.loads(result.stdout)
            except (json.JSONDecodeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                return parsed
        return None
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        LOG.error("Failed to parse subprocess JSON output: %s", exc)
        return None


def _has_worktree_changes(
    worktree_path: str, parent_branch: str = DEFAULT_PARENT_BRANCH
) -> bool:
    """Check if the worktree has uncommitted changes or commits ahead.

    A worktree that is only *behind* the parent branch has no work and is
    reported as clean.

    Args:
        worktree_path: Absolute path to the worktree.
        parent_branch: Branch the worktree forked from (default: ``dev``).

    Returns:
        True if the worktree is dirty or has commits ahead of the parent
        branch.
    """
    return _git_path_has_changes(Path(worktree_path), parent_branch)


def phase_parent(
    work_item_id: str,
    json_output: bool = False,
    no_refactor: bool = False,
    parent_branch: str = DEFAULT_PARENT_BRANCH,
    verbose: bool = False,
) -> dict[str, Any]:
    """Phase: implement a parent work item by recursing into its children.

    Invoking the implement skill on a parent/epic item must automatically
    implement the children (and their blockers) in dependency order instead
    of dead-ending at the old Step 5.1 gate (SA-0MSQBM2FK005NW1T). Each
    invocation of this phase advances the chain by one child:

    1. Fetch the parent and its children (``wl show --children``).
    2. Resolve the parent's **own** outbound (``depends-on``) edges and its
       own status: an unmet same-project blocker, or ``status: blocked``,
       yields a dedicated soft ``blocked`` verdict (``parent_blocked_by``,
       no ``next_child``, no worktree) — the producer's sequencing hold is
       never overridden (SA-0MVS1QS9J009DGPT).
    3. No children → behave like a leaf (no status change; use start/finish).
    4. Resolve the dependency graph among children (outbound
       ``wl dep list`` edges): in-chain blockers are implemented before
       the children they block; cycles fail fast; cross-project blockers
       are reported as coordination notes.
    5. Classify children: terminal → skip; a live lock holder →
       ``skipped-held`` (report the holder and stop cleanly); in_progress
       elsewhere → skip+report; blocked by a non-terminal sibling or by a
       same-project item outside the epic → not startable (reported);
       otherwise → implementable.
    6. All children terminal → advance the parent to ``completed``/``in_review``
       (existing Step 5.1 advancement retained) and comment a per-child summary.
    7. Otherwise → start the next startable child (claim + worktree via
       ``phase_start``) and report the worktree path; the caller implements
       that child with the standard workflow (``implement.py finish
       <child>``), then re-invokes this phase for the next child.

    Worktree isolation is preserved per child: every child is implemented in
    its own worktree created by ``phase_start`` (never the main checkout);
    sequential children reuse/rotate the same ``.worklog/worktrees``
    machinery. No worktree is created for the parent itself.

    Args:
        work_item_id: The parent work item ID.
        json_output: If True, output JSON.
        no_refactor: If True, skip the refactor step for started children.
        parent_branch: Parent branch for child worktrees.
        verbose: Enable verbose logging.

    Returns:
        Dict with the recursion plan: per-child classifications, the next
        child started (with its worktree), or parent advancement when all
        children are terminal. When the invoked item is itself blocked, the
        report carries ``blocked: true``, ``parent_blocked_by`` (blocker ids
        and statuses), ``parent_blocked_reason`` and no ``next_child`` /
        ``parent_advanced`` — a soft wait, not a failure (SA-0MVS1QS9J009DGPT).
    """
    report: dict[str, Any] = {
        "phase": "parent",
        "work_item_id": work_item_id,
        "success": True,
        "children": [],
        "message": "",
    }

    # ── Step 1: Validate work item ID ──────────────────────────────
    if not WORK_ITEM_ID_PATTERN.match(work_item_id):
        msg = f"Invalid work item ID format: {work_item_id}. Expected pattern like SA-XXXXXXXXXXX"
        report["success"] = False
        report["message"] = msg
        if json_output:
            print(format_json_output(report))
        else:
            LOG.error(msg)
        return report

    # ── Step 2: Code Freeze gate (before claiming/starting children) ──
    if is_code_freeze_active():
        msg = (
            "Project is in Code Freeze — implementation blocked until the "
            "release completes."
        )
        LOG.error(msg)
        report["success"] = False
        report["message"] = msg
        report["code_freeze"] = True
        if json_output:
            print(format_json_output(report))
        else:
            print(f"\n⛔ {msg}\n")
        return report

    # ── Step 3: Fetch parent + children ────────────────────────────
    parent = wl_show(work_item_id)
    if not parent:
        msg = f"Work item {work_item_id} not found or failed to fetch"
        report["success"] = False
        report["message"] = msg
        if json_output:
            print(format_json_output(report))
        else:
            LOG.error(msg)
        return report
    children = wl_show_children(work_item_id)

    # ── Step 3.5: Outbound (depends-on) gate for the invoked item ──
    # The parent's OWN outbound edges and its own ``blocked`` status are
    # producer sequencing decisions. Both are resolved BEFORE any child is
    # classified, so a deliberately blocked epic can never have its first
    # startable child started — which used to create a worktree and edit files
    # a still-pending dependency owns (SA-0MVS1QS9J009DGPT). The verdict is a
    # dedicated soft ``blocked`` report (``blocked``/``parent_blocked_by``, no
    # ``next_child``, no ``parent_advanced``, ``success`` true), so callers can
    # tell a legitimate wait from a hard failure. Assumption A1: a plain
    # ``blocked`` status with no outbound edges also waits, mirroring
    # ``wl next``'s blocked-item semantics.
    outbound = _outbound_gate(work_item_id)
    parent_status = str(parent.get("status", ""))
    status_blocked = parent_status == "blocked"
    if outbound["coordination_blockers"]:
        LOG.info(
            "Cross-project dependency note(s) for %s: %s",
            work_item_id,
            _format_blockers(outbound["coordination_blockers"]),
        )
        report["coordination_blockers"] = outbound["coordination_blockers"]
    if outbound["blocked"] or status_blocked:
        reasons = []
        details = []
        if outbound["blocked"]:
            reasons.append(PARENT_BLOCKED_REASON_DEPENDENCY)
            details.append(
                "unmet outbound dependency: "
                f"{_format_blockers(outbound['blockers'])}"
            )
        if status_blocked:
            reasons.append(PARENT_BLOCKED_REASON_STATUS)
            if outbound["blockers"]:
                details.append("parent status is 'blocked'")
            else:
                # ``wl dep add`` sets ``status: blocked`` automatically and
                # nothing clears it when the dependency completes, so a
                # status with no unmet recorded dependency may be stale —
                # never silently override it, but name the remedy.
                details.append(
                    "parent status is 'blocked' (no unmet recorded "
                    f"dependency explains it; clear it with `wl update "
                    f"{work_item_id} --status open` if the hold no longer "
                    "applies)"
                )
        msg = (
            f"{work_item_id} is blocked — " + " and ".join(details) + ". "
            "No child was started and no worktree was created. Resolve the "
            "blocker(s) (or clear the blocked status) and re-run."
        )
        LOG.warning(msg)
        report["success"] = True  # soft stop: a blocked wait is not a failure
        report["blocked"] = True
        report["parent_blocked_by"] = outbound["blockers"]
        report["parent_blocked_reason"] = "+".join(reasons)
        report["parent_status"] = parent_status
        # Classifications are skipped: every child is reported as held by the
        # parent-level block, never as startable.
        report["children"] = [
            {
                "id": str(child.get("id", "")),
                "title": child.get("title", ""),
                "status": child.get("status", ""),
                "action": ACTION_BLOCKED_PARENT,
            }
            for child in children
        ]
        report["children_processed"] = []
        report["message"] = msg
        if json_output:
            print(format_json_output(report))
        else:
            print()
            print("=" * 60)
            print(f"  \u26d4 {msg}")
            print("=" * 60)
            print()
        return report

    # ── Step 4: No children → behave like a leaf ───────────────────
    if not children:
        report["leaf"] = True
        report["message"] = (
            f"Work item {work_item_id} has no children — behave as a leaf "
            "item: use `implement.py start/finish` as usual (no recursion)."
        )
        if json_output:
            print(format_json_output(report))
        else:
            print()
            print("=" * 60)
            print(f"  Parent: {work_item_id} — no children (leaf)")
            print("=" * 60)
            print(f"  {report['message']}")
            print()
        return report

    # ── Step 5: Classify children ──────────────────────────────────
    classifications: list[dict[str, Any]] = []
    blockers_map: dict[str, list[dict[str, Any]]] = {}
    # Same-project blockers OUTSIDE the children set: they cannot be ordered
    # within the epic, but they still gate the child (consistent with the
    # ``phase_start`` leaf gate). Cross-project edges stay coordination notes
    # (SA-0MSQBM2FK005NW1T).
    out_of_chain_blocked: dict[str, list[dict[str, str]]] = {}
    for child in children:
        cid = str(child.get("id", ""))
        # Outbound (depends-on) edges: what this child is blocked by.
        child_blockers = wl_dep_blockers(cid) if cid else []
        blockers_map[cid] = child_blockers
        # Blockers outside the children set (e.g. cross-project) are
        # coordination notes, not in-chain edges (SA-0MSQBM2FK005NW1T).
        in_chain_ids = {str(c.get("id")) for c in children}
        out_of_chain = [
            b for b in child_blockers if str(b.get("id")) not in in_chain_ids
        ]
        external = [
            str(b.get("id"))
            for b in out_of_chain
            if not _is_same_project(cid, str(b.get("id", "")))
        ]
        unmet_same_project = [
            _blocker_payload(b)
            for b in out_of_chain
            if _is_same_project(cid, str(b.get("id", "")))
            and not _is_terminal_child(b)
        ]
        if unmet_same_project:
            out_of_chain_blocked[cid] = unmet_same_project
        classifications.append({
            "id": cid,
            "title": child.get("title", ""),
            "status": child.get("status", ""),
            "action": _classify_child(child),
            "external_blockers": external,
        })
        entry = classifications[-1]
        if unmet_same_project:
            entry["out_of_chain_blockers"] = unmet_same_project
        if entry["action"] == ACTION_SKIPPED_HELD:
            # Surface the live holder's identity so the operator can diagnose
            # who owns the child (AC3).
            entry["held_by"] = _probe_child_lock(cid)
    report["children"] = classifications

    # ── Step 5.5: Resolve dependency order (blockers first) ────────
    ordered_children, cycle_error = _resolve_implementation_order(
        children, blockers_map
    )
    if cycle_error:
        report["success"] = False
        report["message"] = cycle_error
        if json_output:
            print(format_json_output(report))
        else:
            print()
            print("=" * 60)
            print(f"  ⛔ {cycle_error}")
            print("=" * 60)
            print()
        return report

    # ── Step 6: Loop over children — finish implemented, start new ─
    # A single phase_parent invocation walks every child in dependency
    # order, finishing those whose worktrees already contain changes and
    # starting the next unimplemented child. Each child's start/finish runs
    # in its own subprocess (``_invoke_implement``) — process isolation and
    # serial ordering. When a child still needs implementation the loop
    # returns to the caller; when every child is terminal it advances the
    # parent.
    child_results: list[dict[str, Any]] = []
    child_ids = {str(c.get("id")) for c in children}
    terminal_ids = {
        str(c.get("id")) for c in children
        if _is_terminal_child(c)
    }
    pending_ids: list[str] = []
    blocked_ids: list[str] = []

    def _in_chain_blockers(child_id: str) -> list[str]:
        return [
            str(b.get("id"))
            for b in blockers_map.get(child_id, [])
            if str(b.get("id")) in child_ids
        ]

    def _report_implement_next(
        cid: str, worktree_path: str, branch: str, message: str,
        start_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Populate and return the report for a child awaiting implementation."""
        if _forward_child_lock_reclamation(report, cid, start_result):
            message = (
                f"{message} {cid} lock reclaimed from a dead holder "
                "(see warnings)."
            )
        report["next_child"] = cid
        report["worktree_path"] = worktree_path
        report["branch"] = branch
        report["message"] = message
        report["children_processed"] = child_results
        if blocked_ids:
            # Surface the children that are still waiting so the operator can
            # see which siblings the started child is not blocked behind.
            report["blocked_children"] = list(blocked_ids)
        if json_output:
            print(format_json_output(report))
        else:
            print()
            print("=" * 60)
            print(f"  Implement child: {cid} (of {work_item_id})")
            print("=" * 60)
            print(f"  Worktree: {worktree_path}")
            if branch:
                print(f"  Branch:   {branch}")
            for warning in report.get("warnings") or []:
                print(f"  ⚠ {warning}")
            print()
            print("  Next steps:")
            print(f"  1. cd {worktree_path}")
            print("  2. Write tests and implementation code")
            print(f"  3. Re-run: python3 scripts/implement.py parent {work_item_id}")
            print()
        return report

    def _fail_placement(cid: str, violation: str) -> dict[str, Any]:
        """Fail closed when a child's work landed in the main checkout.

        A non-terminal child whose worktree is clean at the parent HEAD while
        the main checkout is dirty means the child wrote outside its worktree
        (``cwd`` is not sufficient). Report the offending paths and stop
        instead of silently proceeding or advancing the parent.
        """
        LOG.error(violation)
        report["success"] = False
        report["message"] = violation
        report["next_child"] = cid
        report["placement_violation"] = True
        report["children_processed"] = child_results
        wl_add_comment(
            work_item_id,
            "Parent phase refused: a child session wrote to the main "
            f"checkout instead of its worktree.\n```\n{violation}\n```",
        )
        if json_output:
            print(format_json_output(report))
        else:
            print(f"\n\u26d4 {violation}\n")
        return report

    def _run_finish(cid: str, title: str = "") -> dict[str, Any] | None:
        """Run the finish subprocess for *cid* (None on failure).

        ``parse_on_failure=True`` surfaces the ``finish`` report's structured
        ``steps`` even when the subprocess exits non-zero, so a failing
        ``finish`` is classified into ``{kind, step, reason}`` here — the
        parent report carries it as ``finish_failure`` for the caller
        (``phase_drive``). The ``None`` sentinel is preserved: a failure —
        parsed report or not — still returns ``None``.
        """
        LOG.info("Child %s has changes — running finish subprocess...", cid)
        finish_result = _invoke_implement(
            "finish", cid, no_refactor=no_refactor, verbose=verbose,
            parse_on_failure=True,
        )
        if finish_result is None or not finish_result.get("success", False):
            msg = f"Failed to finish child {cid}"
            LOG.error(msg)
            # A finish subprocess killed by its timeout bound (or by a signal
            # that never reached a Python exit hook) leaves the child
            # in-progress with nothing to release it. The surviving parent
            # releases that orphaned claim durably before reporting the
            # failure (SA-0MVVF75ZA007XNKB); a child whose own failure path
            # already reset it is left untouched by the helper.
            release = _release_orphaned_child_claim(cid)
            if release.get("released"):
                msg = f"{msg} ({release['reason']})"
            report["success"] = False
            report["message"] = msg
            report["next_child"] = cid
            report["finishing_failed"] = True
            report["finish_failure"] = classify_finish_failure(finish_result)
            report["orphaned_claim_release"] = release
            report["children_processed"] = child_results
            if json_output:
                print(format_json_output(report))
            else:
                print(f"\n\u26a0\ufe0f {msg}\n")
            return None
        terminal_ids.add(cid)
        child_results.append({
            "id": cid, "title": title,
            "action": "finished",
            "status": "completed",
        })
        return finish_result

    for child in ordered_children:
        cid = str(child.get("id", ""))
        title = child.get("title", "")
        action = _classify_child(child)

        if action == "skip-terminal":
            child_results.append({
                "id": cid, "title": title,
                "action": "skipped-terminal",
                "status": child.get("status", ""),
            })
            continue

        if action == ACTION_SKIPPED_HELD:
            # A live holder owns this child's critical section. Stop cleanly
            # with the holder's identity instead of spawning a second session
            # (AC3) — never a concurrent second writer.
            holder = _probe_child_lock(cid)
            child_results.append({
                "id": cid, "title": title,
                "action": ACTION_SKIPPED_HELD,
                "status": child.get("status", ""),
                "held_by": holder,
            })
            report["success"] = True
            report["action"] = ACTION_SKIPPED_HELD
            report["skipped_held"] = True
            report["held_by"] = holder
            report.setdefault("held_children", []).append(cid)
            report["message"] = (
                f"Child {cid} is held by "
                f"{_describe_child_lock_holder(holder)}; no second session "
                f"was started. Re-run `implement.py parent {work_item_id}` "
                "once the holder finishes."
            )
            report["children_processed"] = child_results
            if json_output:
                print(format_json_output(report))
            else:
                print(f"\n\u23f3 {report['message']}\n")
            return report

        worktree_path = _discover_worktree(cid)
        has_changes = bool(
            worktree_path and _has_worktree_changes(worktree_path, parent_branch)
        )

        if action == "skip-in-progress":
            # A started child may be ours (worktree exists) or another
            # agent's claim (no worktree → never clobbered).
            if has_changes:
                if _run_finish(cid, title) is None:
                    return report
            elif worktree_path:
                violation = _child_main_checkout_violation(
                    cid, worktree_path, parent_branch
                )
                if violation:
                    return _fail_placement(cid, violation)
                return _report_implement_next(
                    cid, worktree_path, "",
                    f"Child {cid} is in progress. Implement in "
                    f"{worktree_path}, then re-run "
                    f"`implement.py parent {work_item_id}`.",
                )
            else:
                pending_ids.append(cid)
                child_results.append({
                    "id": cid, "title": title,
                    "action": "skipped-in-progress",
                    "status": child.get("status", ""),
                })
            continue

        # action == "implement" (child is open). A child whose same-project
        # blocker lies OUTSIDE the epic cannot be ordered in-chain and must
        # wait for it (the ``phase_start`` leaf gate would refuse anyway) —
        # report it blocked instead of attempting a start that cannot succeed.
        if out_of_chain_blocked.get(cid):
            pending_ids.append(cid)
            blocked_ids.append(cid)
            child_results.append({
                "id": cid, "title": title,
                "action": "blocked",
                "status": child.get("status", ""),
                "blocked_by": out_of_chain_blocked[cid],
            })
            continue

        # A child is startable only when every in-chain blocker is already
        # terminal (never before its blockers — AC-2).
        if not all(b in terminal_ids for b in _in_chain_blockers(cid)):
            pending_ids.append(cid)
            blocked_ids.append(cid)
            child_results.append({
                "id": cid, "title": title,
                "action": "blocked",
                "status": child.get("status", ""),
            })
            continue

        if has_changes:
            if _run_finish(cid, title) is None:
                return report
            continue

        if worktree_path:
            violation = _child_main_checkout_violation(
                cid, worktree_path, parent_branch
            )
            if violation:
                return _fail_placement(cid, violation)
            return _report_implement_next(
                cid, worktree_path, "",
                f"Child {cid} worktree exists at {worktree_path}. "
                f"Implement, then re-run "
                f"`implement.py parent {work_item_id}`.",
            )

        # No worktree — start the child in its own subprocess.
        LOG.info("Starting child %s of parent %s...", cid, work_item_id)
        start_result = _invoke_implement(
            "start", cid, no_refactor=no_refactor, verbose=verbose,
        )
        if start_result is None:
            msg = f"Failed to start child {cid}"
            LOG.error(msg)
            report["success"] = False
            report["message"] = msg
            report["next_child"] = cid
            report["start_failed"] = True
            report["children_processed"] = child_results
            if json_output:
                print(format_json_output(report))
            else:
                print(f"\n\u26a0\ufe0f {msg}\n")
            return report

        wt = start_result.get("worktree_path", "")
        return _report_implement_next(
            cid, wt, start_result.get("branch", ""),
            f"Started child {cid}. Implement in {wt}, then "
            f"re-run `implement.py parent {work_item_id}`.",
            start_result=start_result,
        )

    # ── Step 7: Advance only when every child is terminal ──────────
    if pending_ids:
        report["message"] = (
            f"No child of {work_item_id} is currently startable: "
            f"non-terminal children not in progress are blocked by "
            f"non-terminal siblings or another agent holds them. Re-run "
            f"this phase after one completes."
        )
        if blocked_ids:
            report["blocked_children"] = blocked_ids
        report["children_processed"] = child_results
        if json_output:
            print(format_json_output(report))
        else:
            print()
            print("=" * 60)
            print(f"  ⏳ {report['message']}")
            print("=" * 60)
            print()
        return report

    parent_status = str(parent.get("status", ""))
    already_terminal = _is_terminal_status(parent_status)
    summary = "\n".join(
        f"- {c['id']} ({c['status']})" for c in classifications
    )
    child_summary = "\n".join(
        f"- {r.get('id', '?')}: {r.get('action', '?')} "
        f"({r.get('status', '')})"
        for r in child_results
    )
    # ── Comment BEFORE the terminal write ─────────────────────────
    # Design A (SA-0MVXUSED6008J04R §3): in **every** implement path the
    # terminal ``completed``/``in_review`` write is the last lifecycle action,
    # so the per-child summary is posted first. A comment bumps
    # ``updatedAt``, so posting it after the transition would make the newly
    # audit-ready parent churn once more (and can invalidate a fresh audit's
    # window, SA-0MTHC710X003ORZM). No persisted audit can exist on a
    # non-terminal parent, so this reordering is behaviour-neutral.
    if not already_terminal:
        wl_add_comment(
            work_item_id,
            f"All children of {work_item_id} are in a terminal stage. "
            f"Advancing the parent to in_review.\n\n"
            f"Children processed:\n{child_summary}\n\n"
            f"Per-child summary:\n{summary}",
        )
        try:
            # Direct terminal durable write (phase_parent is not wrapped in a
            # StatusLifecycle context manager): the release must stay explicit
            # so the epic is never left on an agent hold (assignee-hold
            # convention).
            StatusLifecycle.update_status(
                work_item_id, "completed", stage="in_review", assignee=""
            )
        except RuntimeError:
            msg = f"Failed to advance parent {work_item_id}"
            # The summary above promised an advance this write could not
            # deliver: correct the record rather than leave a false claim.
            wl_add_comment(
                work_item_id,
                f"Parent advancement failed: the terminal status write did "
                f"not stick. {work_item_id} was left non-terminal — re-run "
                f"`implement.py parent {work_item_id}`.",
            )
            report["success"] = False
            report["message"] = msg
            if json_output:
                print(format_json_output(report))
            else:
                LOG.error(msg)
            return report
    else:
        # Already terminal: no lifecycle write follows, so the summary is
        # posted here without promising another transition.
        wl_add_comment(
            work_item_id,
            f"All children of {work_item_id} are in a terminal stage. "
            f"Parent is already in a terminal state ({parent_status}).\n\n"
            f"Children processed:\n{child_summary}\n\n"
            f"Per-child summary:\n{summary}",
        )
    report["parent_advanced"] = True
    report["message"] = (
        f"All children of {work_item_id} are terminal; parent advanced "
        f"to completed/in_review."
    )
    if already_terminal:
        report["message"] = (
            f"All children of {work_item_id} are terminal; parent is "
            f"already in a terminal state ({parent_status})."
        )
    report["children_processed"] = child_results
    if json_output:
        print(format_json_output(report))
    else:
        print()
        print("=" * 60)
        print(f"  ✅ Parent advanced: {work_item_id} → in_review")
        print("=" * 60)
        print(summary)
        print()
    return report


def _child_session_env(
    base: dict[str, str] | None = None,
    worktree_path: str | None = None,
) -> dict[str, str]:
    """Build the environment for a driven child session.

    Marks the session as driver-owned (recursion guard), removes the
    parent session's identity so the child starts its own clean session,
    propagates the per-child lock ownership token, and (when *worktree_path*
    is given) injects ``IMPLEMENT_WORKTREE_PATH`` so the child can verify its
    worktree root before its first edit.

    Args:
        base: Base environment (defaults to ``os.environ``).
        worktree_path: Absolute path to the child's worktree. When provided,
            exported as ``IMPLEMENT_WORKTREE_PATH``.

    Returns:
        A new environment mapping for the child process.
    """
    env = dict(base if base is not None else os.environ)
    env[DRIVE_RECURSION_ENV] = "1"
    env.pop("PI_SESSION_ID", None)
    env.pop("PI_SESSION_FILE", None)
    # Explicit ownership propagation (AC4): the driven session runs
    # ``start``/``finish`` for the same child id, so it must re-enter the
    # holder chain's lock by token rather than be refused by it. Never a pid
    # heuristic — a tokenless session is a different owner.
    token = _child_lock_token(env)
    if token:
        env[ENV_LOCK_TOKEN] = token
    if worktree_path:
        env[WORKTREE_ENV] = worktree_path
    return env


def _resolve_child_session_timeout() -> int:
    """Resolve the per-child session timeout in seconds for ``phase_drive``.

    ``IMPLEMENT_DRIVE_CHILD_TIMEOUT`` overrides the default. The test-command
    timeout is not reused directly because a child session runs the whole
    implement workflow (changed-scope tests, the dev-push full-suite gate,
    and the pre-push hook) — several test runs, not one.

    Returns:
        Timeout in seconds.
    """
    raw = os.environ.get("IMPLEMENT_DRIVE_CHILD_TIMEOUT")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            LOG.warning(
                "Ignoring invalid IMPLEMENT_DRIVE_CHILD_TIMEOUT=%r", raw,
            )
    return DEFAULT_CHILD_SESSION_TIMEOUT


def _drive_infra_backoff_delay(attempt: int) -> int:
    """Bounded exponential backoff (seconds) for infrastructure drive retries.

    Yields ``min(2 * 2**(attempt-1), 30)`` — ``[2, 4, 8, 16, 30, 30, …]``
    seconds for attempts 1, 2, 3, … — so a transient refusal that clears
    within the infra-retry budget (``--infra-retry-limit``) is retried with
    bounded, escalating spacing and a contended host is never hammered
    (LP-0MVWEP292000ODBS AC6).

    Args:
        attempt: The 1-based retry attempt number.

    Returns:
        Seconds to wait before the retry, capped at 30.
    """
    return min(2 * 2 ** (attempt - 1), 30)


def _resolve_pi_bin(env: dict[str, str] | None = None) -> str | None:
    """Resolve the Pi executable used to spawn child sessions.

    Args:
        env: Environment mapping (defaults to ``os.environ``).

    Returns:
        Absolute path to the Pi executable, or ``None`` when unavailable.
    """
    env_map = env if env is not None else os.environ
    configured = env_map.get("IMPLEMENT_DRIVE_PI_BIN") or "pi"
    return shutil.which(configured, path=env_map.get("PATH"))


def _tail_lines(text: str, count: int) -> str:
    """Return the last *count* non-empty lines of *text*, joined by ' | '."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-count:]) if lines else ""


def _default_session_spawner(
    child_id: str,
    worktree_path: str,
    timeout: int,
    env: dict[str, str],
    verbose: bool = False,
) -> dict[str, Any]:
    """Spawn a fresh Pi session to implement *child_id*.

    Runs ``pi -p "/skill:implement <child_id>" --approve --no-extensions`` as
    a separate process with a clean context window, waits for it to exit, and
    reports success. The child session runs the standard implement skill; its
    ``start`` phase is idempotent so the pre-created worktree is resumed.

    ``--no-extensions`` disables extension discovery in the child so a
    globally installed ``turn_end`` handler cannot trip Pi core's boundary
    dispatch — see the inline comment below and
    ``docs/dev/implement-skill-reference.md``.

    A ``timeout`` (or non-zero exit) result is **not** a verdict: the caller
    (``phase_drive``) reconciles it against the child's actual work-item state
    via :func:`_reconcile_spawn_failure`, because the child may have committed,
    pushed and reached a terminal state before the session was killed
    (SA-0MVHXWLPG007OQVN). This function deliberately reports only what
    happened to the process.

    Args:
        child_id: The child work item ID.
        worktree_path: The child's pre-created worktree (process cwd).
        timeout: Maximum seconds to wait for the child session.
        env: Environment for the child process (already marked with the
            recursion guard — see :func:`_child_session_env`).
        verbose: Pass ``--verbose`` to the child Pi invocation.

    Returns:
        ``{"success": bool, "returncode": int | None, "reason": str}``.
    """
    result: dict[str, Any] = {
        "success": False,
        "returncode": None,
        "reason": "",
    }
    pi_bin = _resolve_pi_bin(env)
    if not pi_bin:
        result["reason"] = (
            "No Pi executable available (looked for 'pi' on PATH). Set "
            "IMPLEMENT_DRIVE_PI_BIN to the Pi binary, or drive the children "
            f"manually with `python3 {Path(__file__).resolve()} parent` and "
            "`implement.py finish <child>`."
        )
        return result

    # ``--no-extensions`` keeps extension-provided ``turn_end`` handlers out of
    # the headless child session. ``ExtensionRunner.hasHandlers("turn_end")``
    # is *global across all loaded extensions*, and two installed extensions
    # register one (SorraAgents ``pi-client/proxy-sse-signals`` and the
    # ContextHub ``Worklog`` recovery extension), so guarding a single handler
    # cannot silence the problem. When Pi core's
    # ``AgentSession._dispatchTurnEndBoundary`` cannot resolve the finishing
    # assistant message to a persisted session entry while any ``turn_end``
    # handler exists, it emits ``Extension error (<boundary>): turn_end could
    # not resolve the persisted assistant entry ID`` on the child's stderr.
    # In the ``tce-main-street`` epic (MS-0MUXW90YE009L4DQ) a single driven
    # child emitted 191 identical boundary lines and nothing else, drowning
    # the captured output that ``drive`` uses for failure diagnosis. The
    # identity mismatch itself is a Pi-core bug (no fixed release to upgrade
    # to) and cannot be patched from this repo. ``audit_runner.py`` already
    # runs every headless audit ``pi`` call with ``--no-extensions`` for the
    # same reason (SA-0MUEIOGT2005IR7F) — this mirrors that precedent
    # (SA-0MUY9PYBD009Y7J5). ``--no-extensions`` does not affect skills, which
    # load via ``--no-skills``, so ``/skill:implement`` still expands.
    cmd = [
        pi_bin,
        "-p",
        f"/skill:implement {child_id}",
        "--approve",
        "--no-extensions",
    ]
    if verbose:
        cmd.append("--verbose")
    LOG.info(
        "Spawning child session for %s (cwd=%s): %s",
        child_id, worktree_path, " ".join(cmd),
    )
    try:
        proc = subprocess.run(
            cmd,
            cwd=worktree_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        result["reason"] = (
            f"Child session for {child_id} timed out after {timeout}s."
        )
        return result
    except OSError as exc:
        result["reason"] = (
            f"Failed to spawn child session for {child_id}: {exc}"
        )
        return result

    result["returncode"] = proc.returncode
    if proc.returncode != 0:
        tail = _tail_lines(proc.stderr or proc.stdout or "", 5)
        result["reason"] = (
            f"Child session for {child_id} exited with code "
            f"{proc.returncode}: {tail}"
        )
        return result
    result["success"] = True
    return result


def _env_flag(env: Mapping | None, name: str) -> bool:
    """True when *name* is set to a truthy value (``1``/``true``/``yes``/``on``).

    Args:
        env: Environment mapping (defaults to ``os.environ``).
        name: Environment variable name.

    Returns:
        True when the variable holds a truthy string, else False.
    """
    value = (env if env is not None else os.environ).get(name)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _pane_sessions_opted_in(
    flag: bool | None = None,
    env: Mapping | None = None,
) -> bool:
    """Whether the operator opted in to the pane-driven transport.

    An explicit ``--pane-sessions`` / ``--no-pane-sessions`` flag always wins
    (``--no-pane-sessions`` is an opt-out even when the env var is set). When
    the flag is absent the decision is env-driven via
    :data:`PANE_SESSIONS_ENV`.

    Args:
        flag: Parsed CLI flag (``None`` = not given).
        env: Environment mapping (defaults to ``os.environ``).

    Returns:
        True when pane driving is opted in.
    """
    if flag is not None:
        return bool(flag)
    return _env_flag(env, PANE_SESSIONS_ENV)


def _herdr_available(env: Mapping | None = None) -> bool:
    """Whether a live Herdr session with a resolvable CLI is present.

    Detection is deliberately two-part (AC5): ``HERDR_ENV=1`` (Herdr exports it
    into every managed pane) **and** a resolvable herdr binary (reusing
    :func:`shared.herdr_pane.resolve_herdr_bin`, so ``HERDR_BIN_PATH`` wins
    over ``PATH`` exactly as it does for pane titles). Anything else falls
    back to the headless spawner.

    Args:
        env: Environment mapping (defaults to ``os.environ``).

    Returns:
        True when pane driving *can* work in this environment.
    """
    env_map = dict(env if env is not None else os.environ)
    if env_map.get("HERDR_ENV") != "1":
        return False
    return bool(_resolve_herdr_bin_path(env_map))


def _select_session_spawner(
    pane_sessions: bool | None = None,
    focus: bool = DEFAULT_PANE_FOCUS,
    env: Mapping | None = None,
    default: Callable[..., dict[str, Any]] | None = None,
    client: Any | None = None,
) -> Callable[..., dict[str, Any]]:
    """Choose the child-session transport for this ``drive`` invocation.

    Pane driving is selected only when the operator opted in **and** Herdr is
    detected; every other combination returns *default* (the headless
    ``pi -p`` spawner) unchanged. This fail-open rule is what keeps automated
    callers — the downtime worker, audit dispatch and CI, none of which have a
    Herdr session — on the headless path even if a stray flag is passed.

    Args:
        pane_sessions: Parsed ``--pane-sessions``/``--no-pane-sessions`` flag
            (``None`` = env-driven via :data:`PANE_SESSIONS_ENV`).
        focus: Whether each new child pane takes focus (``--focus``).
        env: Environment mapping used for detection (defaults to
            ``os.environ``).
        default: The headless spawner to fall back to (tests may inject one).
        client: Injectable herdr session client bound into the pane spawner
            (tests); ignored by the headless path.

    Returns:
        A spawner callable with the ``_default_session_spawner`` signature.
    """
    base = default or _default_session_spawner
    if not _pane_sessions_opted_in(pane_sessions, env):
        return base
    if not _herdr_available(env):
        LOG.info(
            "Pane-driven sessions requested but Herdr is not available "
            "(HERDR_ENV=1 and a resolvable herdr binary are required); "
            "falling back to the headless `pi -p` spawner."
        )
        return base
    return functools.partial(
        _pane_session_spawner, focus=bool(focus), client=client,
    )


def _pane_child_env(env: Mapping | None) -> dict[str, str]:
    """The explicit environment a pane child receives via ``pane split --env``.

    Only the deliberate markers (:data:`PANE_CHILD_ENV_KEYS`) are forwarded —
    never the whole inherited environment (which would leak secrets into the
    recorded argv) and never the driving session's identity
    (:data:`PANE_IDENTITY_ENV_KEYS`).

    Args:
        env: The child environment built by :func:`_child_session_env`.

    Returns:
        Ordered ``{KEY: value}`` pairs, empty entries omitted.
    """
    env_map = env if env is not None else os.environ
    pairs: dict[str, str] = {}
    for key in PANE_CHILD_ENV_KEYS:
        if key in PANE_IDENTITY_ENV_KEYS:
            continue
        value = env_map.get(key)
        if value:
            pairs[key] = str(value)
    return pairs


def _pane_session_spawner(
    child_id: str,
    worktree_path: str,
    timeout: int,
    env: dict[str, str],
    verbose: bool = False,
    *,
    focus: bool = DEFAULT_PANE_FOCUS,
    client: Any | None = None,
) -> dict[str, Any]:
    """Drive *child_id* as an interactive Pi TUI agent in a sibling pane.

    The pane-driven counterpart of :func:`_default_session_spawner`
    (SA-0MVPHYIIE002OBC8):

    1. ``pane split --current --cwd <worktree> --env K=V… [--focus|--no-focus]``
       — a sibling pane in the caller's tab, never a new tab/workspace.
    2. ``agent start drive-<child> --kind pi --pane <id> -- --approve
       --no-extensions`` — the same extensions-disabled child session shape
       as the headless path.
    3. ``agent prompt <name> "/skill:implement <child>" --wait --until done
       --until blocked`` — the interactive turn that runs the leaf workflow.

    The child environment is the one built by :func:`_child_session_env`: the
    pane receives the recursion guard, the worktree marker and the lock token,
    but not the driving session's ``PI_SESSION_ID`` / ``PI_SESSION_FILE``.
    Because ``herdr agent start`` has no unset-environment option, the same
    scrubbed environment is handed to the herdr CLI itself (the ``env -u
    PI_SESSION_ID -u PI_SESSION_FILE`` equivalent), so the launched TUI cannot
    inherit the driving session's identity from this process either.

    Every herdr call is fail-open: a split/start/prompt failure is reported as
    ``success: False`` with the herdr reason, never raised, and the chain stops
    cleanly. When the pane was created the returned ``pane_id`` lets the caller
    retain (or close) it — pane lifecycle handling is the next slice's job.

    Args:
        child_id: The child work item ID.
        worktree_path: The child's pre-created worktree (the pane's cwd).
        timeout: Per-child session budget in seconds (bounds readiness and the
            prompt wait).
        env: The child environment (already scrubbed by
            :func:`_child_session_env`).
        verbose: Accepted for signature parity with the headless spawner.
        focus: Whether the new pane takes focus (default: do not steal focus).
        client: Injectable herdr session client (tests); defaults to a
            :class:`shared.herdr_session.HerdrSessionClient` over *env*.

    Returns:
        ``{"success", "returncode", "reason", "pane_id", "transport"}`` —
        a superset of :func:`_default_session_spawner`'s result, so existing
        ``phase_drive`` success handling is unchanged.
    """
    # Accepted for signature parity with ``_default_session_spawner``; the
    # pane transport logs its own argv.
    del verbose
    result: dict[str, Any] = {
        "success": False,
        "returncode": None,
        "reason": "",
        "pane_id": None,
        "transport": "pane",
    }

    herdr = client or HerdrSessionClient(env=env)

    split = herdr.split_pane(worktree_path, env=_pane_child_env(env), focus=focus)
    if not split.get("success"):
        result["reason"] = (
            f"Failed to split a Herdr pane for {child_id}: "
            f"{split.get('reason') or 'unknown herdr failure'}"
        )
        return result

    pane_id = str(split["pane_id"])
    result["pane_id"] = pane_id
    agent_name = agent_name_for_child(child_id)
    result["agent_name"] = agent_name

    LOG.info(
        "Driving child %s in Herdr pane %s as agent %s (cwd=%s)",
        child_id, pane_id, agent_name, worktree_path,
    )

    # herdr caps the readiness wait at 300s; a larger child budget must not be
    # clamped into an invalid CLI argument.
    readiness_ms = min(max(int(timeout), 1) * 1000, 300_000)
    start = herdr.start_agent(
        agent_name,
        pane_id,
        args=["--approve", "--no-extensions"],
        timeout_ms=readiness_ms,
        ready_budget_seconds=min(float(max(int(timeout), 1)), 30.0),
    )
    if not start.get("success"):
        result["returncode"] = start.get("returncode")
        result["reason"] = (
            f"Failed to start the pane agent for {child_id}: "
            f"{start.get('reason') or 'unknown herdr failure'}"
        )
        return result

    prompt = herdr.prompt_agent(
        agent_name,
        f"/skill:implement {child_id}",
        wait=True,
        until=("done", "blocked"),
        timeout_ms=max(int(timeout), 1) * 1000,
    )
    result["returncode"] = prompt.get("returncode")
    result["agent_status"] = prompt.get("agent_status")
    if not prompt.get("success"):
        reason = prompt.get("reason") or "unknown herdr failure"
        if prompt.get("code") == "timeout":
            # Wording matters: ``_reconcile_spawn_failure`` classifies a
            # timeout by the phrase and reconciles against the child's real
            # state, so a child that finished before the wait expired is
            # reported as completed-late, not failed.
            reason = (
                f"Pane agent for {child_id} timed out after {timeout}s: "
                f"{reason}"
            )
        else:
            reason = f"Pane agent for {child_id} did not complete: {reason}"
        result["reason"] = reason
        return result

    LOG.info(
        "Pane agent %s for %s settled with status %s",
        agent_name, child_id, result["agent_status"],
    )
    result["success"] = True
    result["returncode"] = prompt.get("returncode") or 0
    return result


def _pane_lifecycle_client(client: Any | None = None) -> Any:
    """The herdr client the driver uses to close/title panes it created.

    The pane spawner builds its own client from the scrubbed child
    environment; the driver only needs the *caller's* environment (the pane id
    and binary are ambient there). Tests inject a recording client, which is
    returned unchanged.

    Args:
        client: Injectable client (tests); ``None`` builds a default one.

    Returns:
        A ``HerdrSessionClient``-compatible object.
    """
    return client if client is not None else HerdrSessionClient()


def _driven_pane_fields(spawn_result: Mapping[str, Any]) -> dict[str, Any]:
    """The ``transport``/``pane_id`` report fields for a driven child entry.

    Report parity (AC5): every ``children_driven`` entry carries both keys, so
    a pane-driven child is distinguishable from a headless one in the human
    summary and in ``--json``. A spawner that predates the pane transport (or
    a test double) reports neither: the transport then defaults to
    ``headless`` and ``pane_id`` is ``None``.

    Args:
        spawn_result: The spawner's result dict.

    Returns:
        ``{"transport": str, "pane_id": str | None}``.
    """
    transport = str(spawn_result.get("transport") or TRANSPORT_HEADLESS)
    pane_id = spawn_result.get("pane_id")
    return {
        "transport": transport,
        "pane_id": str(pane_id) if pane_id else None,
    }


def _pane_inspect_remedy(pane_id: str) -> str:
    """The copy-pasteable command that inspects a retained pane."""
    return f"herdr pane read {pane_id}"


def _retain_driven_pane(
    child_id: str,
    pane_id: str,
    verdict: str,
    entry: dict[str, Any],
    report: dict[str, Any],
    reason: str,
    *,
    title: bool = True,
) -> dict[str, Any]:
    """Keep a drive-created pane open, report it and title its verdict.

    A pane the driver created is never leaked silently: it is either closed
    once its child reached a terminal stage
    (:func:`_close_driven_pane`) or listed in ``report["retained_panes"]``
    with an actionable inspection remedy (AC3). The title is set through
    ``herdr_pane.update_pane_title`` (fail-open), so an operator watching the
    pane grid sees the ``⚠️``/``🚫`` verdict without opening the child's log.

    Args:
        child_id: The child work item the pane belongs to.
        pane_id: The pane to retain.
        verdict: ``herdr_pane`` status (``attention`` / ``note``).
        entry: The ``children_driven`` entry to annotate.
        report: The drive report (receives ``retained_panes``).
        reason: Why the pane is retained.
        title: Whether to apply the verdict title. ``False`` for a pane the
            drive ends without a verdict for (nothing to announce).

    Returns:
        The ``retained_panes`` entry that was recorded.
    """
    pane = str(pane_id)
    entry["pane_id"] = pane
    entry["pane_retained"] = pane
    entry["pane_closed"] = False
    if title:
        try:
            entry["pane_title"] = _update_pane_title(verdict, pane_id=pane)
        except Exception as exc:  # noqa: BLE001 - pane updates are fail-open
            LOG.debug("Pane title update for %s failed: %s", pane, exc)
            entry["pane_title"] = {"updated": False, "reason": str(exc)}
    retained = {
        "id": child_id,
        "pane_id": pane,
        "verdict": verdict,
        "reason": reason,
        "remedy": _pane_inspect_remedy(pane),
    }
    report.setdefault("retained_panes", []).append(retained)
    LOG.warning(
        "Retained pane %s for %s (%s): %s",
        pane, child_id, verdict, reason,
    )
    return retained


def _close_driven_pane(
    child_id: str,
    pane_id: str,
    entry: dict[str, Any],
    report: dict[str, Any],
    client: Any | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """Close a drive-created pane whose child reached a terminal stage.

    Fail-open: a refused/failed close retains the pane and reports it (with
    its remedy) instead of raising, so a herdr hiccup never breaks the drive
    chain — and the pane is never leaked silently (AC3).

    Args:
        child_id: The child work item the pane belongs to.
        pane_id: The pane to close.
        entry: The ``children_driven`` entry to annotate.
        report: The drive report (receives ``retained_panes`` on failure).
        client: Injectable herdr client (tests); defaults to the ambient one.
        reason: Why the pane is closed (recorded in the report entry).

    Returns:
        The ``pane_cleanup`` outcome recorded on *entry*.
    """
    pane = str(pane_id)
    close_reason = reason or "child reached a terminal stage"
    try:
        outcome = _pane_lifecycle_client(client).close_pane(pane)
    except Exception as exc:  # noqa: BLE001 - never break the drive chain
        outcome = {"success": False, "reason": f"close failed: {exc}"}

    entry["pane_id"] = pane
    if outcome.get("success"):
        entry["pane_closed"] = True
        entry["pane_cleanup"] = {
            "pane_id": pane,
            "closed": True,
            "reason": close_reason,
        }
        LOG.info("Driver closed pane %s for %s (%s)", pane, child_id, close_reason)
        return entry["pane_cleanup"]

    failure = str(outcome.get("reason") or "unknown herdr failure")
    entry["pane_cleanup"] = {
        "pane_id": pane,
        "closed": False,
        "reason": failure,
    }
    _retain_driven_pane(
        child_id,
        pane,
        PANE_VERDICT_FAILED,
        entry,
        report,
        reason=f"pane close failed: {failure}",
    )
    return entry["pane_cleanup"]


def _resolve_pending_pane_closes(
    parent_report: dict[str, Any] | None,
    pending: dict[str, str],
    report: dict[str, Any],
    client: Any | None = None,
) -> None:
    """Close queued child panes once the parent reports their state.

    The spawn-success path queues the child's pane instead of re-fetching the
    child: the next ``phase_parent`` report's ``children_processed`` entry is
    the terminality signal (the same contract the deferred worktree removal
    uses). A terminal child's pane is closed; a child the parent still sees
    non-terminal keeps its pane open and is reported as retained (AC3).

    Args:
        parent_report: The latest ``phase_parent`` report.
        pending: ``{child_id: pane_id}`` awaiting a terminal verdict.
        report: The drive report (receives ``retained_panes``).
        client: Injectable herdr client (tests).
    """
    processed: dict[str, dict[str, Any]] = {}
    for item in (parent_report or {}).get("children_processed") or []:
        cid = str(item.get("id") or "")
        if cid:
            processed[cid] = item

    for child_id in list(pending):
        info = processed.get(child_id)
        if info is None:
            continue  # the parent has not reported this child yet
        status = str(info.get("status") or "")
        action = str(info.get("action") or "")
        pane_id = pending.pop(child_id)
        entry = next(
            (e for e in report.get("children_driven", []) if e.get("id") == child_id),
            None,
        )
        if entry is None:  # pragma: no cover - defensive
            entry = {}
        if not (_is_terminal_status(status) or action in ("finished", "skipped-terminal")):
            _retain_driven_pane(
                child_id,
                pane_id,
                PANE_VERDICT_FAILED,
                entry,
                report,
                reason="child is not terminal; pane kept open for inspection",
            )
            continue
        _close_driven_pane(
            child_id,
            pane_id,
            entry,
            report,
            client,
            reason="child reached a terminal stage",
        )


def _retain_pending_panes(pending: dict[str, str], report: dict[str, Any]) -> None:
    """Report queued panes the drive ended before resolving.

    A drive that stops (failure, blocked child, advanced parent) before the
    follow-up ``parent`` report named a child must not forget the pane it
    created: it is listed in ``retained_panes`` with its inspection remedy so
    the operator can find it (AC3). No verdict title is applied — the drive
    has nothing to announce about the session yet.
    """
    for child_id, pane_id in pending.items():
        entry = next(
            (e for e in report.get("children_driven", []) if e.get("id") == child_id),
            None,
        )
        if entry is None:  # pragma: no cover - defensive
            entry = {}
        _retain_driven_pane(
            child_id,
            pane_id,
            PANE_VERDICT_FAILED,
            entry,
            report,
            reason=(
                "drive ended before the parent reported this child's state; "
                "inspect or close the pane"
            ),
            title=False,
        )


def _reconcile_spawn_failure(
    child_id: str,
    spawn_result: dict[str, Any],
    timeout: int | None = None,
) -> dict[str, Any]:
    """Classify a failed child session against the child's current state.

    A per-child session timeout (or non-zero exit) is a *bound*, not a
    verdict: the child may already have committed, pushed and reached a
    terminal work-item state before the session was killed. In the
    ``WL-0MVGEB3IP002V8IB`` epic every child did exactly that, yet ``drive``
    reported a hard failure and stopped the epic chain
    (SA-0MVHXWLPG007OQVN).

    This helper re-fetches the child (read-only ``wl show``) and classifies it
    with :func:`_is_terminal_child`. It is called **only** on the spawn-failure
    path (risk R1): a successful spawn never pays the extra fetch.

    Terminal means status ``completed``/``done``/``in_review`` (or a
    soft-deleted item) — see :data:`TERMINAL_STATUSES` — or a work item whose
    stage is already ``in_review`` (the workflow sets both atomically, but a
    stage-only terminal state is still terminal for reconciliation purposes).
    A child that is still ``open``/``blocked``/``in_progress`` is **not**
    terminal: the pre-existing hard-failure behaviour applies (risk R2).

    Args:
        child_id: The child work item ID whose session failed.
        spawn_result: The spawner's result dict (``success``/``reason``).
        timeout: The per-child session timeout in seconds (for the evidence).

    Returns:
        A dict with ``terminal`` (bool), ``status``, ``stage``, ``timed_out``
        (bool), ``timeout_seconds`` (int | None), ``evidence`` (str) and
        ``reason`` (str). ``status``/``stage`` are empty when the child could
        not be fetched — callers must treat that as non-terminal (fail closed).
    """
    raw_reason = str(spawn_result.get("reason", "") or "")
    timed_out = "timed out" in raw_reason.lower()
    result: dict[str, Any] = {
        "terminal": False,
        "status": "",
        "stage": "",
        "timed_out": timed_out,
        "timeout_seconds": timeout if timed_out else None,
        "evidence": "",
        "reason": raw_reason,
    }

    child = wl_show(child_id)
    if not child:
        # Fail closed: an unreadable child state is never a silent success.
        LOG.warning(
            "Could not re-fetch %s after a failed child session; keeping the "
            "hard failure.",
            child_id,
        )
        return result

    status = str(child.get("status", "") or "")
    stage = str(child.get("stage", "") or "")
    result["status"] = status
    result["stage"] = stage
    result["terminal"] = _is_terminal_child(child) or stage == "in_review"
    if not result["terminal"]:
        return result

    result["evidence"] = f"status={status}, stage={stage}"
    result["reason"] = (
        f"{raw_reason} Child reached a terminal state anyway "
        f"({result['evidence']})."
    )
    LOG.info(
        "Reconciled %s after a failed child session: terminal (%s).",
        child_id,
        result["evidence"],
    )
    return result


def _release_orphaned_child_claim(child_id: str) -> dict[str, Any]:
    """Release a child claim orphaned by a killed or timed-out subprocess.

    A ``finish`` subprocess (or a driven child session) that is killed by a
    timeout bound — or by a signal that never reaches a Python exit hook —
    cannot run its own release, so the *surviving* supervisor performs it with
    a durable write: the assignee is cleared (assignee-hold convention: a
    non-empty assignee is a scheduling hold, so a lingering one makes the item
    invisible to ``wl next``). This is the timeout counterpart of
    :func:`_safety_reset_if_in_progress`, which covers the signal path
    (SA-0MVVF75ZA007XNKB).

    Only an ``in-progress`` child is touched, and only its assignee is
    cleared: the status is deliberately left alone so the existing recovery
    paths still apply — ``phase_parent`` re-finishes a child whose worktree
    holds the work, and ``drive --finalise-stranded`` still recognises the
    child as ``in-progress``. Every other state holds no claim this workflow
    owns, so a producer ``Map`` hold or a terminal item is never disturbed,
    and a child whose own failure path already reset it is left alone — the
    helper is therefore idempotent and never masks the original failure (the
    caller keeps reporting it).

    Args:
        child_id: The child work item whose claim may be orphaned.

    Returns:
        ``{"released": bool, "status": str, "previous_status": str,
        "previous_assignee": str, "reason": str}``. ``released`` is ``False``
        when there was nothing to release or the release write failed.
    """
    result: dict[str, Any] = {
        "released": False,
        "status": "",
        "previous_status": "",
        "previous_assignee": "",
        "reason": "",
    }
    child = wl_show(child_id)
    if not child:
        result["reason"] = "could not re-fetch the child work item"
        return result

    status = str(child.get("status", "") or "")
    assignee = str(child.get("assignee", "") or "")
    result["previous_status"] = status
    result["previous_assignee"] = assignee
    result["status"] = status
    if status not in ("in-progress", "in_progress"):
        result["reason"] = (
            f"child is not in-progress (status={status!r}); no claim to release"
        )
        return result
    if not assignee:
        result["reason"] = "child holds no assignee; nothing to release"
        return result

    # ``wl`` spells the status ``in-progress``; the read-back can carry the
    # underscore form, so normalise before writing it back unchanged.
    kept_status = "in-progress" if status == "in_progress" else status
    try:
        StatusLifecycle.update_status(child_id, kept_status, assignee="")
    except (RuntimeError, ValueError) as exc:
        result["reason"] = f"release write failed: {exc}"
        return result

    result["released"] = True
    result["reason"] = (
        "assignee released after its session was killed (item stays "
        "in-progress, so the existing finish/finalise recovery still applies)"
    )
    wl_add_comment(
        child_id,
        f"Claim released: the session working on this item was killed "
        f"(timeout or signal) before it could release its own claim, so the "
        f"assignee ({assignee!r}) was cleared by the surviving supervisor. "
        f"The item stays `in-progress` so it can still be finished or "
        f"finalised; any uncommitted work in its worktree is untouched.",
    )
    LOG.info(
        "Released orphaned claim on %s (was status=%s, assignee=%r)",
        child_id, status, assignee,
    )
    return result


def _reconcile_finish_failure(
    child_id: str,
    parent_report: dict[str, Any],
    finish_attempts: int,
) -> dict[str, Any]:
    """Classify a child after its failing ``finish`` exhausted the bound.

    ``implement.py finish`` can fail *after* the child reached a terminal
    state (for example the push succeeded and a later gate step failed).
    Treating every ``finishing_failed`` as chain-fatal stalls an epic whose
    child is genuinely done. This helper re-fetches the child (read-only
    ``wl show``) and classifies it with :func:`_is_terminal_child`
    (SA-0MVLJXTYY005XQDV). It is called **only** when the finish-attempt
    bound is exhausted, so the happy path pays no extra fetch.

    Args:
        child_id: The child whose ``finish`` failed.
        parent_report: The ``phase_parent`` report carrying the failure.
        finish_attempts: Number of failing finish executions observed.

    Returns:
        A dict with ``terminal`` (bool), ``status``, ``stage``,
        ``finish_attempts``, ``evidence`` (str) and ``reason`` (str).
        ``status``/``stage`` are empty when the child could not be fetched —
        callers must treat that as non-terminal (fail closed).
    """
    result: dict[str, Any] = {
        "terminal": False,
        "status": "",
        "stage": "",
        "finish_attempts": finish_attempts,
        "evidence": "",
        "reason": str(parent_report.get("message", "") or ""),
    }

    child = wl_show(child_id)
    if not child:
        LOG.warning(
            "Could not re-fetch %s after %d failed finish attempt(s); "
            "keeping the hard failure.",
            child_id,
            finish_attempts,
        )
        return result

    status = str(child.get("status", "") or "")
    stage = str(child.get("stage", "") or "")
    result["status"] = status
    result["stage"] = stage
    result["evidence"] = f"status={status or '?'}, stage={stage or '?'}"
    result["terminal"] = _is_terminal_child(child) or stage == "in_review"
    if not result["terminal"]:
        return result

    result["reason"] = (
        f"finish failed {finish_attempts} time(s) but the child reached a "
        f"terminal state anyway ({result['evidence']})."
    )
    LOG.info(
        "Reconciled %s after a failed finish: terminal (%s).",
        child_id,
        result["evidence"],
    )
    return result


def _worktree_ahead_of_parent(
    worktree_path: Path, parent_branch: str,
) -> tuple[bool | None, int | None]:
    """Whether the worktree's ``HEAD`` carries commits the parent lacks.

    The preservation gate's ahead-of-parent check: a worktree whose ``HEAD``
    carries commits the parent branch does not have would lose them to a
    ``git reset --hard <parent_branch>``, so the reset must be refused
    (parent AC3, LP-0MVWEP2KB002WMRY).

    Primary computation: ``git rev-list --count <parent_branch>..HEAD`` — a
    positive count means the child's own commits would be lost. Falls back to
    ``git merge-base --is-ancestor <parent_branch> HEAD`` when the count
    cannot be computed (e.g. an unresolvable parent ref), guarded against the
    ``HEAD == parent`` equality case so an exactly-at-parent worktree is never
    misclassified as ahead.

    **Tri-state, fail-closed.** ``None`` means undecidable (the parent ref
    does not resolve, or ``HEAD`` is unreadable). A destroyer must never
    interpret "unknown" as "safe": the caller refuses the reset on ``None``
    rather than discarding commits it could not enumerate.

    Args:
        worktree_path: The linked worktree path (git runs with ``-C``).
        parent_branch: The parent branch to measure against (``dev``).

    Returns:
        ``(is_ahead, count)`` where ``is_ahead`` is ``True``/``False`` when
        decided and ``None`` when undecidable; ``count`` is the number of
        commits ahead when the primary computation succeeded, otherwise
        ``None``.
    """
    count = run_cmd(
        [
            "git", "-C", str(worktree_path), "rev-list", "--count",
            f"{parent_branch}..HEAD",
        ],
        check=False,
    )
    if count.returncode == 0:
        text = count.stdout.strip()
        if text.isdigit():
            n = int(text)
            return n > 0, n
    # Fallback: is the parent branch an ancestor of HEAD? (A == B passes, so
    # guard the equality case explicitly — an exactly-at-parent worktree is
    # never "ahead".)
    parent_rev = run_cmd(
        ["git", "-C", str(worktree_path), "rev-parse", "--verify", "--quiet",
         f"{parent_branch}^{{commit}}"],
        check=False,
    )
    head_rev = run_cmd(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"], check=False,
    )
    if parent_rev.returncode != 0 or head_rev.returncode != 0:
        return None, None
    if parent_rev.stdout.strip() == head_rev.stdout.strip():
        return False, None
    ancestor = run_cmd(
        ["git", "-C", str(worktree_path), "merge-base", "--is-ancestor",
            parent_branch, "HEAD"],
        check=False,
    )
    if ancestor.returncode == 0:
        return False, None
    if ancestor.returncode == 1:
        return True, None
    return None, None


def _status_porcelain_paths(worktree_path: str) -> list[str]:
    """Return the paths ``git status --porcelain=v1`` reports (read-only).

    Used by :func:`_reset_child_worktree` to capture the uncommitted work a
    reset is about to discard **before** the destructive commands run, so the
    reset-for-retry audit comment can name the discarded files exactly
    (parent AC4, LP-0MVWEP2VQ001NPRK). Untracked directories are expanded to
    individual files (``--untracked-files=all``) so the list names files
    rather than a directory that a reader cannot audit. A rename/copy entry
    is reported by its destination path.

    Args:
        worktree_path: The worktree to read (never mutated).

    Returns:
        A sorted, de-duplicated list of repository-relative paths. An
        unreadable worktree or a failing ``git status`` (e.g. a missing
        path) yields ``[]`` — the caller never crashes on a diagnostic read.
    """
    status = run_cmd(
        [
            "git", "-C", str(worktree_path), "status", "--porcelain=v1",
            "--untracked-files=all",
        ],
        check=False,
    )
    if status.returncode != 0:
        return []
    paths: list[str] = []
    for line in (status.stdout or "").splitlines():
        if len(line) < 4:
            continue
        entry = line[3:].strip()
        if not entry:
            continue
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        paths.append(entry.strip('"'))
    return sorted(set(paths))


#: A 7–40 character hex run that a reader could mistake for a commit hash.
_COMMIT_HASH_TOKEN_RE = re.compile(r"\b[0-9a-f]{7,40}\b")

#: Placeholder used while protecting verbatim path tokens from redaction.
_REDACTION_SENTINEL = "\x00{}\x00"


def _redact_commit_hash_tokens(
    text: str, keep: tuple[str, ...] | list[str] = (),
) -> str:
    """Replace commit-hash-shaped tokens in *text* with a neutral marker.

    The reset-for-retry comment must never imply that a **commit** was
    discarded: it is the record of an uncommitted-only reset, so any
    hash-shaped token (which a reader would read as a commit) is redacted.
    Tokens listed in *keep* (the discarded file list) are left verbatim, so
    the evidence-based file list stays byte-accurate even when a filename
    happens to look like a hash (parent AC2/AC3, LP-0MVWEP2VQ001NPRK).

    Args:
        text: The text to sanitise.
        keep: Tokens to preserve verbatim (paths from the discard list).

    Returns:
        The sanitised text.
    """
    protected = [token for token in keep if token]
    text = text or ""
    for index, token in enumerate(protected):
        text = text.replace(token, _REDACTION_SENTINEL.format(index))
    text = _COMMIT_HASH_TOKEN_RE.sub("<hash redacted>", text or "")
    for index, token in enumerate(protected):
        text = text.replace(_REDACTION_SENTINEL.format(index), token)
    return text


def _reset_child_worktree(
    worktree_path: str,
    parent_branch: str = DEFAULT_PARENT_BRANCH,
    child_id: str = "",
) -> dict[str, Any]:
    """Reset a child's **own** worktree to the parent-branch HEAD.

    Used by the drive recovery path after a child's ``finish`` exhausted its
    attempts: the abandoned WIP leaves a dirty worktree that makes every
    subsequent drive re-run the identical failing ``finish`` (the livelock
    reproduced on AH-0MV9DB68T002IROX). Resetting the worktree to the
    parent-branch HEAD restores a clean startable state so the next loop
    iteration can start a fresh session (SA-0MVLJXVND0015LPW).

    Isolation gates — the reset only ever touches the child's own worktree:

    1. the path exists and is a **linked git worktree** (``.git`` is a file);
       a main checkout (``.git`` directory) is refused outright;
    2. ``git rev-parse --show-toplevel`` inside the path resolves to the path
       itself, so a nested/shared path can never be reset;
    3. only ``git reset --hard <parent_branch>`` and ``git clean -fd`` run —
       the clean is never run on a shared/main path.

    **Preservation gate** (parent AC3, LP-0MVWEP2KB002WMRY) — runs after the
    isolation gates and **before** any destructive command: when the worktree
    carries commits ahead of the parent branch (``_worktree_ahead_of_parent``),
    the tip is parked on ``refs/wip/<child-id>`` with ``git update-ref`` and
    the reset is refused without running ``git reset --hard`` / ``git clean
    -fd``. A commit-carrying worktree is therefore never reset, no matter how
    many finish attempts failed — the committed slice cannot be discarded.

    ``git clean -fd`` deliberately omits ``-x``: ignored files (notably the
    ``node_modules`` symlink) survive the reset.

    Args:
        worktree_path: The child's worktree path (never the main checkout).
        parent_branch: Ref to reset to (default: ``dev``).
        child_id: The child work-item id; names the preservation ref
            (``refs/wip/<child-id>``). When empty, a commit-carrying worktree
            is still refused — fail-safe, nothing destructive runs.

    Returns:
        A dict with ``success`` (bool), ``reset`` (bool), ``ref`` (the ref
        reset to) and ``reason`` (failure explanation, empty on success). On
        a refused preservation the dict additionally carries ``preserved``
        (True), ``preserved_ref`` (``refs/wip/<child-id>``) and
        ``preserved_commit`` (the worktree tip). On a successful reset the
        dict additionally carries ``commits_ahead`` (always 0 — the
        uncommitted-only invariant, parent AC4 / LP-0MVWEP2VQ001NPRK) and
        ``discarded_paths`` — the pre-reset ``git status --porcelain=v1``
        file list (one entry per path), so the reset-for-retry audit comment
        is evidence-based rather than inferred from ``git clean`` stdout.
    """
    result: dict[str, Any] = {
        "success": False,
        "reset": False,
        "ref": parent_branch,
        "reason": "",
    }
    if not worktree_path:
        result["reason"] = "no worktree path available for the child"
        return result

    wt = Path(worktree_path)
    if not wt.exists():
        result["reason"] = f"worktree path does not exist: {worktree_path}"
        return result
    if not _is_worktree(wt):
        result["reason"] = (
            f"refusing to reset {worktree_path}: it is not a linked git "
            "worktree (main-checkout reset is forbidden)"
        )
        return result

    top = run_cmd(
        ["git", "-C", str(wt), "rev-parse", "--show-toplevel"], check=False,
    )
    top_path = Path(top.stdout.strip()) if top.stdout.strip() else None
    if (
        top.returncode != 0
        or top_path is None
        or top_path.resolve() != wt.resolve()
    ):
        result["reason"] = (
            f"refusing to reset {worktree_path}: git reports a different "
            "worktree root"
        )
        return result

    # Preservation gate: a worktree that carries commits ahead of the parent
    # branch is never reset. Park the tip on refs/wip/<child-id> BEFORE any
    # destructive command and refuse (parent AC3, LP-0MVWEP2KB002WMRY).
    ahead, ahead_count = _worktree_ahead_of_parent(wt, parent_branch)
    if ahead is None:
        result["reason"] = (
            f"refusing to reset {worktree_path}: could not determine whether "
            f"the worktree is ahead of {parent_branch} (unresolvable ref or "
            "unreadable HEAD); a destroyer must not assume 'safe'"
        )
        return result
    if ahead:
        tip = run_cmd(
            ["git", "-C", str(wt), "rev-parse", "HEAD"], check=False,
        )
        tip_commit = tip.stdout.strip() if tip.returncode == 0 else ""
        if not child_id:
            result["reason"] = (
                f"refusing to reset {worktree_path}: the worktree is "
                f"{ahead_count if ahead_count is not None else '1+'}"
                f" commit(s) ahead of {parent_branch} and no child id was "
                "given to name a preservation ref; committed work would be "
                "discarded"
            )
            return result
        if not tip_commit:
            result["reason"] = (
                f"refusing to reset {worktree_path}: could not resolve the "
                "worktree HEAD; committed work would be discarded"
            )
            return result
        preserved_ref = f"refs/wip/{child_id}"
        update_ref = run_cmd(
            ["git", "-C", str(wt), "update-ref", preserved_ref, tip_commit],
            check=False,
        )
        if update_ref.returncode != 0:
            result["reason"] = (
                f"refusing to reset {worktree_path}: could not create "
                f"preservation ref {preserved_ref} "
                f"({(update_ref.stderr or '').strip()}); committed work "
                "would be discarded"
            )
            return result
        result["success"] = False
        result["reset"] = False
        result["preserved"] = True
        result["preserved_ref"] = preserved_ref
        result["preserved_commit"] = tip_commit
        result["reason"] = (
            f"refusing to reset {worktree_path}: the worktree is "
            f"{ahead_count if ahead_count is not None else '1+'}"
            f" commit(s) ahead of {parent_branch}; the tip was preserved "
            f"at {preserved_ref} ({tip_commit}) and no destructive command "
            "ran"
        )
        return result

    # Pre-reset uncommitted file list: the evidence the reset-for-retry audit
    # comment reports (parent AC4, LP-0MVWEP2VQ001NPRK). Captured here, after
    # the preservation gate and BEFORE any destructive command, so it describes
    # exactly what the reset is about to discard.
    discarded_paths = _status_porcelain_paths(str(wt))

    reset = run_cmd(
        ["git", "-C", str(wt), "reset", "--hard", parent_branch],
        check=False,
    )
    if reset.returncode != 0:
        result["reason"] = (
            f"git reset --hard {parent_branch} failed: "
            f"{(reset.stderr or '').strip()}"
        )
        return result

    clean = run_cmd(["git", "-C", str(wt), "clean", "-fd"], check=False)
    if clean.returncode != 0:
        result["reason"] = (
            f"git clean -fd failed: {(clean.stderr or '').strip()}"
        )
        return result

    result["success"] = True
    result["reset"] = True
    # Uncommitted-only invariant (parent AC4, LP-0MVWEP2VQ001NPRK): the
    # preservation gate refused a commit-carrying worktree above, so a
    # successful reset never discarded commits. Recorded explicitly so the
    # drive path can fail closed if that ever stops holding.
    result["commits_ahead"] = 0
    # The pre-reset status --porcelain file list: exactly what the reset
    # removed (uncommitted paths only), for the audit comment.
    result["discarded_paths"] = discarded_paths
    result["cleaned"] = (clean.stdout or "").strip()
    return result


def _reset_for_retry_comment(
    child_id: str,
    finish_attempts: int,
    failure_message: str,
    reconciliation: dict[str, Any],
    reset_result: dict[str, Any],
    finish_failure: dict[str, Any] | None = None,
) -> str:
    """Build the audit comment recording a reset-for-retry recovery.

    The comment is the durable record that a child owner's **uncommitted**
    WIP was discarded and why: the finish failure reason (with the classified
    failing ``finish`` step/reason when available), the number of failed
    finish attempts, the child's non-terminal state, the exact reset
    performed, and the file list the reset discarded.

    ``reset-for-retry`` is limited to a worktree that carried **no** commits
    ahead of the parent branch (parent AC4, LP-0MVWEP2VQ001NPRK): committed
    work is preserved by the refusal gate in :func:`_reset_child_worktree`,
    never discarded. The comment therefore states explicitly that no commit
    was discarded, and no hash-shaped token can survive in it — a reader can
    never infer that a commit was thrown away. The discarded paths come from
    the pre-reset ``git status --porcelain`` capture
    (``reset_result["discarded_paths"]``), so the list is evidence-based
    rather than inferred from ``git clean`` stdout.

    Args:
        child_id: The child whose worktree was reset.
        finish_attempts: Number of failing finish executions observed.
        failure_message: The parent report's failure message.
        reconciliation: The non-terminal reconciliation payload.
        reset_result: The :func:`_reset_child_worktree` result (carrying
            ``ref``, ``discarded_paths`` and ``cleaned``).
        finish_failure: The classified ``finish_failure`` payload, if any.

    Returns:
        The comment body.
    """
    discarded = list(reset_result.get("discarded_paths") or [])
    ref = reset_result.get("ref")
    failure = finish_failure if isinstance(finish_failure, dict) else {}
    lines = [
        f"Drive reset this child for a clean retry (`{child_id}`).",
        "",
        f"- Failed `finish` attempts: {finish_attempts}",
        (
            f"- Child state before reset: "
            f"{reconciliation.get('evidence') or 'unknown'}"
        ),
        (
            "- Failure reason: "
            f"{failure_message or 'unspecified'}"
        ),
    ]
    if failure.get("step"):
        lines.append(
            f"- Failing `finish` step: `{failure.get('step')}` "
            f"(reason: `{failure.get('reason') or 'unspecified'}`)"
        )
        desc = _describe_finish_failure(failure)
        if desc:
            lines.append(f"- Finish evidence: {desc}")
    lines.extend([
        (
            f"- Worktree reset to: `{ref}` "
            "(`git reset --hard`, then `git clean -fd`)"
        ),
        (
            "- Committed work discarded: none — no commit was discarded "
            f"(the worktree was not ahead of `{ref}`, so there were no "
            "commits to discard)."
        ),
        "",
    ])
    if discarded:
        lines.append(
            f"Discarded uncommitted paths ({len(discarded)}):"
        )
        lines.extend(f"  - {path}" for path in discarded)
    else:
        lines.append(
            "Discarded uncommitted paths: none (the worktree was clean)."
        )
    lines.extend([
        "",
        (
            "Uncommitted WIP in the child's worktree was discarded so the "
            "next drive iteration can start a fresh session. Restore it from "
            "the abandoned session's log if it is still needed."
        ),
    ])
    # Redact hash-shaped tokens (paths excepted) so the discard record can
    # never be read as "a commit was thrown away" (AC2).
    return _redact_commit_hash_tokens("\n".join(lines), keep=discarded)


def _reset_for_retry_violation(reset_result: dict[str, Any]) -> str:
    """Return why a reset result may not be recorded as ``reset-for-retry``.

    ``reset-for-retry`` is limited to a worktree that carried **no commits**
    ahead of the parent branch (parent AC4, LP-0MVWEP2VQ001NPRK). Committed
    work is preserved by the refusal gate in :func:`_reset_child_worktree`
    (tip parked on ``refs/wip/<child-id>``) and never discarded, so a reset
    result that carries commits — or a preservation refusal — must never be
    turned into a ``reset-for-retry`` entry: that would imply the committed
    slice was discarded when it was not.

    Args:
        reset_result: The :func:`_reset_child_worktree` result.

    Returns:
        An explanation when the result violates the invariant, else ``""``.
    """
    if reset_result.get("preserved_ref") or reset_result.get("preserved"):
        return (
            "the worktree carries commits and its tip was preserved at "
            f"{reset_result.get('preserved_ref') or '(unknown ref)'}; a "
            "commit-preserving refusal is never a reset-for-retry"
        )
    commits_ahead = reset_result.get("commits_ahead")
    if commits_ahead:
        return (
            f"the worktree was {commits_ahead} commit(s) ahead of the parent "
            "branch, so the reset would have discarded committed work"
        )
    return ""


def _describe_finish_failure(finish_failure: dict[str, Any]) -> str:
    """Render the failing ``finish`` step/reason plus report evidence.

    The single message-shape formatter for drive verdicts
    (LP-0MVWEP37K000JU0O, parent AC5): returns ``"<step>: <reason>"`` — e.g.
    ``local_dev_fast_forward: local_dev_diverged`` — and appends the
    main-checkout evidence the failing ``finish`` report carries:

    - ``"; lock held by <holder>"`` from ``steps.main_checkout_lock.holder``;
    - ``"; offending paths: <paths>; competing work item <id> (pid <n>)"``
      from the guard's offending paths and concurrent-actor evidence
      (``steps.main_checkout_guard.verifies[-1]``).

    Every field comes from the structured ``finish_failure`` payload, never
    from ``report["message"]`` prose. Empty when the failure was not
    classified into a structured step — the callers then leave their message
    unchanged (fail-safe).

    Args:
        finish_failure: The classified ``finish_failure`` payload, if any.

    Returns:
        ``"<step>: <reason>"`` plus the evidence suffixes when the report
        carries them, else ``""``.
    """
    if not isinstance(finish_failure, dict):
        return ""
    step = finish_failure.get("step")
    if not step:
        return ""
    reason = finish_failure.get("reason")
    parts = [f"{step}: {reason or 'unspecified'}"]
    steps = finish_failure.get("steps")
    if isinstance(steps, dict):
        lock = steps.get("main_checkout_lock")
        holder = lock.get("holder") if isinstance(lock, dict) else None
        if holder:
            parts.append(f"lock held by {holder}")
        guard = steps.get("main_checkout_guard")
        if isinstance(guard, dict):
            verifies = guard.get("verifies") or []
            last = verifies[-1] if verifies else None
            if isinstance(last, dict):
                offending = last.get("offending") or []
                if offending:
                    paths = ", ".join(str(p) for p in offending)
                    parts.append(f"offending paths: {paths}")
                actor = _competing_actor_suffix(
                    last.get("actor_evidence") or ""
                )
                if actor:
                    parts.append(actor)
    return "; ".join(parts)


def _competing_actor_suffix(actor_evidence: str) -> str:
    """Render the guard's concurrent-actor evidence, ``""`` when absent.

    The evidence string names the competing work item and pid in the shape
    ``work item <id> (…); pid <n> with cwd in the main checkout``
    (:func:`_main_checkout_actor_evidence`); those two fields are surfaced
    as ``competing work item <id> (pid <n>)``. Any other evidence shape is
    passed through verbatim so the message never loses information, and
    ``"unknown"`` yields nothing.
    """
    if not actor_evidence or actor_evidence == "unknown":
        return ""
    item = re.search(r"work item (\S+)", actor_evidence)
    pid = re.search(r"pid (\d+)", actor_evidence)
    if item and pid:
        wid = item.group(1).rstrip(".,;:")
        return f"competing work item {wid} (pid {pid.group(1)})"
    return f"competing actor: {actor_evidence}"


def _finish_evidence_suffix(finish_failure: dict[str, Any]) -> str:
    """Render the failing-``finish`` evidence as a message suffix.

    The chain-stop messages append this inside their trailing parentheses so
    the stopping verdict names the real failing ``finish`` step and reason —
    never the generic ``Failed to finish child <id>`` alone
    (LP-0MVWEP37K000JU0O AC1/AC3, parent AC5). Empty when the failure was
    not classified.
    """
    desc = _describe_finish_failure(finish_failure)
    return f"; failing finish step/reason: {desc}" if desc else ""


def _preserved_ref_comment(
    child_id: str,
    preserved: dict[str, Any],
    reconciliation: dict[str, Any],
    finish_failure: dict[str, Any] | None = None,
) -> str:
    """Build the durable record that a commit-carrying worktree was preserved.

    A reset was *refused*, not performed: the child's committed slice is still
    on its branch and its tip is parked on ``refs/wip/<child-id>``. The
    comment names the ref and commit so the owner can recover the work, and
    states explicitly that nothing was discarded (the inverse of
    :func:`_reset_for_retry_comment`).

    Args:
        child_id: The child whose worktree was preserved.
        preserved: The ``{id, ref, commit}`` preservation entry.
        reconciliation: The non-terminal reconciliation payload.
        finish_failure: The classified ``finish_failure`` payload, if any.

    Returns:
        The comment body.
    """
    failure = finish_failure if isinstance(finish_failure, dict) else {}
    step = failure.get("step") or "unknown"
    reason = failure.get("reason") or "unknown"
    desc = _describe_finish_failure(failure)
    step_line = (
        f"- Failing `finish` step/reason: {desc}"
        if desc else
        f"- Failing `finish` step: `{step}` (reason: `{reason}`)"
    )
    lines = [
        (
            f"Drive refused to reset this child's worktree (`{child_id}`) "
            "because it carries commits."
        ),
        "",
        f"- Worktree tip preserved at: `{preserved.get('ref')}`",
        f"- Preserved commit: `{preserved.get('commit')}`",
        f"- Child state: {reconciliation.get('evidence') or 'unknown'}",
        step_line,
        "",
        (
            "No `git reset --hard` or `git clean -fd` was run, so no committed "
            "work was discarded. Recover the slice from the preserved ref "
            f"(`git branch <name> {preserved.get('ref')}`), or delete it once it "
            f"is no longer needed (`git update-ref -d {preserved.get('ref')}`)."
        ),
    ]
    return "\n".join(lines)


def _emit_phase_report(report: dict[str, Any], json_output: bool) -> None:
    """Emit a drive report as JSON or a concise human summary."""
    if json_output:
        print(format_json_output(report))
        return
    print()
    print("=" * 60)
    if report.get("success"):
        print(f"  ✅ Drive complete: {report.get('work_item_id')}")
    else:
        print(f"  ⚠  Drive stopped: {report.get('work_item_id')}")
    print("=" * 60)
    for entry in report.get("children_driven", []):
        result = entry.get("result")
        if result == "finished":
            icon = "✅"
        elif result == "completed-late":
            # The session was killed at the per-child timeout but the child
            # had already reached a terminal state: a distinct, non-failure
            # verdict so a producer is never misled (SA-0MVLJWOJ8004ABBA).
            icon = "🕓"
        elif result == "finalised":
            # A stranded child was finalised through the evidence-gated
            # --finalise-stranded path (SA-0MVLJWTGP005XJPP).
            icon = "🛠"
        elif result == "terminal-reconciled":
            # The child's finish failed but the child had already reached a
            # terminal state: recorded as done, never reset
            # (SA-0MVLJXTYY005XQDV).
            icon = "🕓"
        elif result == "reset-for-retry":
            # A non-terminal child's abandoned worktree was reset to the
            # parent-branch HEAD so a fresh session can retry it
            # (SA-0MVLJXVND0015LPW).
            icon = "♻"
        elif result == "infra-retry":
            # A transient infrastructure refusal retried on its own bounded
            # budget with backoff (LP-0MVWEP292000ODBS).
            icon = "🔁"
        elif result == "blocked":
            # The driven agent settled in `blocked` and needs a human
            # decision; its pane was left open (SA-0MVPHZ1CI0045QR0 AC2).
            icon = "🚫"
        else:
            icon = "❌"
        print(
            f"  {icon} {entry.get('id')} "
            f"(attempt {entry.get('attempt')}): {result}"
        )
        # Report parity (AC5): the transport and, for pane-driven children,
        # the pane the session ran in.
        transport = entry.get("transport")
        pane_id = entry.get("pane_id")
        if transport or pane_id:
            pane_note = f", pane {pane_id}" if pane_id else ""
            print(
                f"     transport: {transport or TRANSPORT_HEADLESS}"
                f"{pane_note}"
            )
        evidence = _format_timeout_evidence(entry)
        if evidence:
            print(f"     {evidence}")
        if entry.get("message"):
            # The verdict's finish-evidence line (``<step>: <reason>`` plus
            # lock/guard evidence) — LP-0MVWEP37K000JU0O AC5.
            print(f"     {entry['message']}")
        if entry.get("reason"):
            print(f"     {entry['reason']}")
    print(f"  Sessions spawned: {report.get('sessions_spawned', 0)}")
    if report.get("completed_late"):
        print(f"  Completed late: {report['completed_late']}")
    if report.get("terminal_reconciled"):
        print(
            f"  Terminal-reconciled: {report['terminal_reconciled']}"
        )
    if report.get("reset_for_retry"):
        print(f"  Reset for retry: {report['reset_for_retry']}")
    if report.get("preserved_refs"):
        print(f"  Preserved (never reset): {len(report['preserved_refs'])}")
        for preserved in report["preserved_refs"]:
            print(
                f"     {preserved.get('id')}: {preserved.get('ref')} "
                f"({preserved.get('commit')})"
            )
    if report.get("infra_retries"):
        print(f"  Infra retries: {report.get('infra_retries')}")
    if report.get("finalised_children"):
        print(f"  Finalised stranded: {len(report['finalised_children'])}")
    if report.get("stranded_children"):
        print(f"  Stranded (needs finalising): {len(report['stranded_children'])}")
    if report.get("retained_panes"):
        print(
            "  Panes retained for inspection: "
            f"{len(report['retained_panes'])}"
        )
        for pane in report["retained_panes"]:
            print(
                f"     {pane.get('id')}: pane {pane.get('pane_id')} "
                f"({pane.get('verdict')}) — {pane.get('reason', '')}"
            )
            if pane.get("remedy"):
                print(f"       inspect with: {pane['remedy']}")
    print(f"  {report.get('message', '')}")
    print()


def _format_timeout_evidence(entry: dict[str, Any]) -> str:
    """Render the timeout/terminal evidence line for a drive report entry.

    Only entries that carry the evidence fields (spawn-failure paths) produce
    a line; successful entries render nothing extra.

    Args:
        entry: A ``children_driven`` entry.

    Returns:
        A human-readable evidence string, or an empty string.
    """
    if "timed_out" not in entry and "terminal" not in entry:
        return ""
    parts: list[str] = []
    if entry.get("timed_out"):
        seconds = entry.get("timeout_seconds")
        parts.append(
            f"timed out after {seconds}s" if seconds else "timed out"
        )
    if entry.get("terminal"):
        status = entry.get("terminal_status") or "?"
        stage = entry.get("terminal_stage") or "?"
        parts.append(f"terminal (status={status}, stage={stage})")
    else:
        parts.append("child incomplete")
    return "; ".join(parts)


def _git_commit_evidence(
    child_id: str,
    worktree_path: str,
    parent_branch: str = DEFAULT_PARENT_BRANCH,
) -> str | None:
    """Return the child's pushed commit hash on the parent branch, if any.

    Read-only git evidence: the implement workflow commits carry the work-item
    id, so a commit on ``origin/<parent_branch>`` whose message references the
    child means its work was pushed. The remote-tracking ref is used as-is (no
    network fetch) — a stale ref is acceptable evidence on the failure path,
    and fetching would add network latency to an already-failing path.

    Degrades gracefully: a missing/unreadable worktree, a git error, or a
    repository without the remote-tracking ref returns ``None`` ("unknown")
    without raising, so detection never crashes the drive loop and never
    produces a false positive.

    Args:
        child_id: The child work-item id to look for.
        worktree_path: The child's worktree (git is run with ``-C``).
        parent_branch: Branch the work would have been pushed to (``dev``).

    Returns:
        The commit hash as a string, or ``None`` when no evidence is found.
    """
    if not worktree_path or not Path(worktree_path).exists():
        return None
    for ref in (f"origin/{parent_branch}", parent_branch):
        try:
            result = run_cmd(
                [
                    "git", "-C", str(worktree_path), "log", ref,
                    "--grep", child_id, "-n", "1", "--format=%H",
                ],
                check=False,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            LOG.debug(
                "Commit-evidence lookup for %s failed against %s: %s",
                child_id, ref, exc,
            )
            continue
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()[0]
    return None


def _detect_stranded_child(
    child_id: str,
    worktree_path: str,
    parent_branch: str = DEFAULT_PARENT_BRANCH,
    commit_evidence: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Detect a child whose work was pushed before its session was killed.

    A stranded child is the ``WL-0MVGUA9EY003XFAD`` production shape: the
    session pushed to ``dev`` but was killed before the final
    ``StatusLifecycle`` terminal update, leaving the item ``in-progress`` and
    blocking ``wl next``. Detection is **read-only** (no ``wl`` mutation, no
    worktree mutation): it only looks for the child's commit on the parent
    branch.

    Args:
        child_id: The child work-item id.
        worktree_path: The child's worktree (for the git evidence).
        parent_branch: Branch the work would have been pushed to.
        commit_evidence: Injectable evidence provider (tests); defaults to
            :func:`_git_commit_evidence`.

    Returns:
        A dict with ``stranded`` (bool), ``commit`` (str), ``evidence`` (str)
        and ``reason`` (str). A provider failure degrades to ``stranded: False``
        (unknown) — never a false positive, never a crash.
    """
    provider = commit_evidence or (
        lambda cid: _git_commit_evidence(cid, worktree_path, parent_branch)
    )
    try:
        commit = provider(child_id)
    except Exception:
        LOG.warning(
            "Commit-evidence provider failed for %s; treating as unknown.",
            child_id, exc_info=True,
        )
        commit = None

    if not commit:
        return {
            "stranded": False,
            "commit": "",
            "evidence": "",
            "reason": (
                f"no commit referencing {child_id} found on {parent_branch}"
            ),
        }
    commit = str(commit)
    return {
        "stranded": True,
        "commit": commit,
        "evidence": f"commit {commit} on {parent_branch}",
        "reason": (
            f"work item {child_id} is not terminal but commit {commit} is "
            f"already on {parent_branch}"
        ),
    }


def _worktree_has_uncommitted_changes(worktree_path: str) -> bool:
    """Whether *worktree_path* holds uncommitted work (read-only check).

    Used as a finalisation safety gate: a stranded child may only be
    finalised automatically when its worktree holds no uncommitted work, so
    automatic finalisation can never strand (or silently discard) work.

    Fail-safe: an unreadable or missing worktree is treated as "has
    changes" (unsafe) so the caller falls back to the read-only diagnostic.

    Args:
        worktree_path: Absolute path to the child's worktree.

    Returns:
        True when the worktree has uncommitted changes or cannot be read.
    """
    if not worktree_path:
        return True
    try:
        result = run_cmd(
            ["git", "-C", str(worktree_path), "status", "--porcelain"],
            check=False,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        LOG.debug("Worktree status check failed for %s: %s", worktree_path, exc)
        return True
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


def _retained_worktree_entry(
    work_item_id: str, worktree_path: str, reason: str
) -> dict[str, Any]:
    """Describe a retained worktree with a copy-pasteable remedy.

    Retained trees are reported (never deleted silently and never forgotten):
    every entry names the path, why it survived and the exact commands that
    remove it (SA-0MVTE8XAJ002IUDR AC7).

    Args:
        work_item_id: The work item that owns the tree.
        worktree_path: Absolute path to the retained worktree.
        reason: Why it was retained.

    Returns:
        A small report entry dict.
    """
    return {
        "id": work_item_id,
        "path": worktree_path,
        "reason": reason,
        "remedy": (
            f"git worktree remove {worktree_path} && git worktree prune"
        ),
    }


def _remove_finished_worktree(
    work_item_id: str,
    worktree_path: str,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Remove a finished item's worktree behind the existing safety gates.

    The single removal path shared by the driver (after a driven child's
    session has exited) and every other deferred-removal caller: no second
    deletion mechanism, no ``rm -rf`` shortcut.

    Gates, in order — a failure at any of them **retains** the tree and is
    reported, never forced past:

    1. the path is known and exists (an absent tree is a reported no-op
       success: ``already_absent``),
    2. the tree holds no uncommitted work
       (:func:`_worktree_has_uncommitted_changes`, untracked included) —
       uncommitted work is never discarded,
    3. :func:`_remove_worktree`'s own gates refuse the repository root and any
       tree with a ``.git/`` directory.

    ``cleanup_worktree_processes`` runs first so a stray ``wl`` process cannot
    block ``git worktree remove`` (its self-exclusion contract is
    SA-0MVJC5KNT0044L2B), and ``remove_state`` runs last so no
    ``.implement_state.json`` is left behind.

    Args:
        work_item_id: The work item that owns the tree.
        worktree_path: Absolute path to the worktree.
        repo_root: The main checkout root (passed to ``_remove_worktree`` so
            its repo-root refusal is authoritative).

    Returns:
        A result dict with ``removed``, ``already_absent``,
        ``retained_dirty``, ``refused``, ``removed_state``, ``path``,
        ``reason`` and (when retained) ``remedy``.
    """
    result: dict[str, Any] = {
        "work_item_id": work_item_id,
        "id": work_item_id,
        "path": worktree_path,
        "removed": False,
        "already_absent": False,
        "retained_dirty": False,
        "refused": False,
        "removed_state": False,
        "reason": "",
    }

    if not worktree_path or not Path(worktree_path).exists():
        # Idempotent: an interrupted/swept run must not fail on a tree that is
        # already gone. ``remove_state`` is a no-op when the file is absent,
        # but it still clears a stale state file left by an odd removal.
        result["already_absent"] = True
        result["reason"] = "worktree already absent"
        if worktree_path:
            remove_state(worktree_path)
        return result

    if _worktree_has_uncommitted_changes(worktree_path):
        result["retained_dirty"] = True
        result["reason"] = "worktree has uncommitted changes"
        result["remedy"] = (
            f"git worktree remove {worktree_path} && git worktree prune"
        )
        return result

    # ``_remove_worktree`` chdirs the calling process out of the tree when cwd
    # is inside it; in the driver that would move the driver's own cwd, so
    # restore it afterwards.
    try:
        cwd_before: str | None = os.getcwd()
    except OSError:  # pragma: no cover - cwd already gone
        cwd_before = None

    cleanup_worktree_processes(worktree_path)
    try:
        removed = bool(_remove_worktree(worktree_path, repo_root=repo_root or None))
    except RuntimeError as exc:
        # A safety-gate refusal (repository root, .git/ directory): retain the
        # tree and report it rather than turning the caller into a failure.
        result["refused"] = True
        result["reason"] = f"removal refused: {exc}"
        result["remedy"] = (
            f"inspect {worktree_path} and remove it manually once safe"
        )
        return result
    finally:
        if cwd_before and Path(cwd_before).exists():
            try:
                os.chdir(cwd_before)
            except OSError:  # pragma: no cover - defensive
                LOG.debug("Could not restore cwd %s", cwd_before)

    result["removed"] = removed
    if removed:
        remove_state(worktree_path)
        result["removed_state"] = not (
            Path(worktree_path) / STATE_FILE_NAME
        ).exists()
        result["reason"] = "worktree removed"
    else:
        result["reason"] = "worktree removal failed"
        result["remedy"] = (
            f"git worktree remove {worktree_path} && git worktree prune"
        )
    return result


def _maybe_remove_child_worktree(
    child_id: str,
    worktree_path: str,
    entry: dict[str, Any],
    report: dict[str, Any],
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Remove a driven child's worktree once terminality is established.

    This is the removal-owner side of the driven flow
    (SA-0MVTE8XAJ002IUDR): a driven child's ``finish`` defers worktree
    removal so the session keeps its cwd, making the driver the owner. The
    caller establishes that the child is **terminal** first (via the next
    ``phase_parent`` report's ``children_processed``, or via the
    spawn-failure reconciliation on the ``completed-late`` path); this helper
    only applies the removal gates:

    - dirty tree ⇒ retained, never discarded;
    - already absent ⇒ reported no-op (``already_absent``);
    - refusal/failure ⇒ retained and reported, the drive chain continues.

    Every retained tree is added to ``report["retained_worktrees"]`` with a
    copy-pasteable remedy. Never raises.

    Args:
        child_id: The child work item id.
        worktree_path: The child's worktree path.
        entry: The ``report["children_driven"]`` entry to record the outcome on.
        report: The drive report (receives ``retained_worktrees``).
        repo_root: The main checkout root (defaults to discovery).

    Returns:
        The :func:`_remove_finished_worktree` result.
    """
    try:
        outcome = _remove_finished_worktree(
            child_id, worktree_path, repo_root or _get_repo_root()
        )
    except Exception as exc:  # noqa: BLE001 - never break the drive chain
        LOG.warning("Worktree cleanup for %s failed: %s", child_id, exc)
        outcome = {
            "id": child_id,
            "path": worktree_path,
            "removed": False,
            "reason": f"cleanup failed: {exc}",
        }

    entry["worktree_removed"] = bool(outcome["removed"])
    entry["worktree_cleanup"] = outcome
    if outcome.get("removed"):
        LOG.info("Driver removed %s's worktree at %s", child_id, worktree_path)
    else:
        entry["worktree_retained"] = worktree_path
        report.setdefault("retained_worktrees", []).append(
            _retained_worktree_entry(
                child_id, worktree_path, outcome.get("reason", "retained")
            )
        )
    return outcome


def _resolve_pending_worktree_cleanups(
    parent_report: dict[str, Any] | None,
    pending: dict[str, str],
    report: dict[str, Any],
    repo_root: str | None = None,
) -> None:
    """Remove queued child worktrees once the parent reports their status.

    The spawn-success path queues the child in *pending* instead of
    re-fetching it (the pinned "reconciliation only on the failure path"
    contract, risk R1 of SA-0MVHXWLPG007OQVN). Every ``phase_parent`` report
    carries ``children_processed`` with each child's status/action, so the
    next report after a finished session names the child — that is the
    terminality signal. Resolving here means the hot path performs **no**
    extra store fetch.

    Args:
        parent_report: The latest ``phase_parent`` report.
        pending: ``{child_id: worktree_path}`` awaiting a terminal verdict.
        report: The drive report (receives ``retained_worktrees``).
        repo_root: The main checkout root (defaults to discovery).
    """
    processed: dict[str, dict[str, Any]] = {}
    for item in (parent_report or {}).get("children_processed") or []:
        cid = str(item.get("id") or "")
        if cid:
            processed[cid] = item

    for child_id in list(pending):
        info = processed.get(child_id)
        if info is None:
            continue  # the parent has not reported this child yet
        status = str(info.get("status") or "")
        action = str(info.get("action") or "")
        worktree_path = pending.pop(child_id)
        entry = next(
            (e for e in report.get("children_driven", []) if e.get("id") == child_id),
            {},
        )
        if not (_is_terminal_status(status) or action in ("finished", "skipped-terminal")):
            # The parent still sees the child non-terminal: retain the tree as
            # debug evidence and never remove it. Reported with its remedy so
            # it is not silently stranded.
            entry["worktree_removed"] = False
            entry["worktree_retained"] = worktree_path
            report.setdefault("retained_worktrees", []).append(
                _retained_worktree_entry(
                    child_id, worktree_path, "child is not terminal"
                )
            )
            continue
        _maybe_remove_child_worktree(
            child_id, worktree_path, entry, report, repo_root=repo_root
        )


def _retain_pending_worktree_cleanups(
    pending: dict[str, str], report: dict[str, Any]
) -> None:
    """Report queued worktrees the drive ended before resolving.

    A drive that stops (failure, blocked parent, advanced parent) before the
    follow-up ``parent`` report named a child leaves that child's queued
    worktree unresolved. It is never silently stranded: it is listed in
    ``report["retained_worktrees"]`` with its remedy, and the next drive's
    start-of-run sweep removes it once the child is terminal.
    """
    for child_id, worktree_path in pending.items():
        entry = next(
            (e for e in report.get("children_driven", []) if e.get("id") == child_id),
            {},
        )
        entry.setdefault("worktree_removed", False)
        entry.setdefault("worktree_retained", worktree_path)
        report.setdefault("retained_worktrees", []).append(
            _retained_worktree_entry(
                child_id,
                worktree_path,
                "drive ended before the parent reported this child's state; "
                "the next drive sweep (or the remedy below) removes it",
            )
        )


def _sweep_terminal_child_worktrees(
    children: list[dict[str, Any]],
    report: dict[str, Any],
    repo_root: str | None = None,
) -> None:
    """Remove leftover worktrees of terminal children at the start of a drive.

    A drive that was interrupted between a child session's exit and the
    driver-side removal strands that child's tree; sweeping at the start of
    the next drive keeps deferred worktrees from leaking (SA-0MVTE8XAJ002IUDR
    AC7). Bounded and best-effort: terminal children only, clean trees only,
    never fatal — the outcome is recorded in ``report["worktree_sweep"]`` and
    retained trees are named in ``report["retained_worktrees"]``.

    Args:
        children: The parent's children (``wl_show_children`` shape).
        report: The drive report to annotate.
        repo_root: The main checkout root (defaults to discovery).
    """
    sweep: list[dict[str, Any]] = []
    for child in children or []:
        if not _is_terminal_child(child):
            continue
        child_id = str(child.get("id") or "")
        if not child_id:
            continue
        try:
            path = _discover_worktree(child_id)
        except Exception as exc:  # noqa: BLE001 - sweep is best-effort
            LOG.debug("Worktree discovery for %s failed: %s", child_id, exc)
            continue
        if not path:
            continue
        try:
            outcome = _remove_finished_worktree(
                child_id, path, repo_root or _get_repo_root()
            )
        except Exception as exc:  # noqa: BLE001 - sweep is best-effort
            LOG.warning("Sweep of %s worktree failed: %s", child_id, exc)
            sweep.append({
                "id": child_id,
                "path": path,
                "removed": False,
                "reason": f"sweep failed: {exc}",
            })
            continue
        LOG.info(
            "Worktree sweep: %s at %s (%s)",
            "removed" if outcome["removed"] else "retained",
            path,
            outcome["reason"],
        )
        sweep.append(outcome)
        if not outcome["removed"]:
            report.setdefault("retained_worktrees", []).append(
                _retained_worktree_entry(child_id, path, outcome["reason"])
            )
    if sweep:
        report["worktree_sweep"] = sweep


def _finalise_stranded_child(
    child_id: str,
    evidence: dict[str, Any],
    assignee: str = "",
) -> dict[str, Any]:
    """Finalise a stranded child via the existing ``StatusLifecycle`` path.

    Safety gates (all must hold — never finalise otherwise):

    1. verified pushed-commit evidence (``evidence['stranded']``),
    2. the child is currently ``in-progress`` (never touch another state),
    3. the child is not claimed by another live agent (non-empty assignee
       other than *assignee*).

    The caller additionally checks the worktree holds no uncommitted work
    (:func:`_worktree_has_uncommitted_changes`) before calling this.

    The mutation goes through ``StatusLifecycle.update_status`` (the only
    supported finalise path) and is recorded as a comment on the child, so
    the automatic finalisation is auditable.

    Args:
        child_id: The stranded child work item id.
        evidence: The :func:`_detect_stranded_child` result.
        assignee: The driving agent's identity; an assignee mismatch means
            another agent holds the item and finalisation is refused.

    Returns:
        A dict with ``finalised`` (bool), ``commit`` (str) and ``reason``
        (str). A refused or failed finalisation returns ``finalised: False``
        so the caller falls back to the read-only diagnostic.
    """
    if not evidence.get("stranded"):
        return {
            "finalised": False,
            "commit": "",
            "reason": "no verified pushed-commit evidence",
        }
    child = wl_show(child_id)
    if not child:
        return {
            "finalised": False,
            "commit": "",
            "reason": "could not re-fetch the child work item",
        }
    status = str(child.get("status", "") or "")
    if status not in ("in-progress", "in_progress"):
        return {
            "finalised": False,
            "commit": "",
            "reason": f"child is not in-progress (status={status!r})",
        }
    claimed_by = str(child.get("assignee", "") or "").strip()
    if claimed_by and claimed_by != assignee:
        return {
            "finalised": False,
            "commit": "",
            "reason": f"child is claimed by {claimed_by!r}",
        }

    commit = str(evidence.get("commit", ""))
    reason = (
        f"Automatically finalised by `implement.py drive --finalise-stranded`: "
        f"the child session failed after its work was pushed "
        f"({evidence.get('evidence', commit)}), the item was still "
        f"in-progress, and its worktree held no uncommitted work. "
        f"Status set to completed / stage in_review."
    )
    # The finalisation comment is posted BEFORE the terminal write, so the
    # write is this path's last lifecycle action (design A,
    # SA-0MVXUSED6008J04R §3: comment before the transition in every implement
    # path). No persisted audit can exist on a non-terminal item, so this
    # reordering is behaviour-neutral.
    wl_add_comment(child_id, reason)
    try:
        # Direct terminal durable write — the finaliser is not a StatusLifecycle
        # exit, so the stranded child's hold is released explicitly.
        StatusLifecycle.update_status(
            child_id, "completed", stage="in_review", assignee=""
        )
    except RuntimeError as exc:
        # The comment above claimed the finalisation; correct the record when
        # the terminal write does not stick rather than leave a false claim.
        wl_add_comment(
            child_id,
            f"Stranded-child finalisation failed: the terminal status write "
            f"did not stick ({exc}). The item was left non-terminal.",
        )
        return {
            "finalised": False,
            "commit": commit,
            "reason": f"status update failed: {exc}",
        }
    LOG.info("Finalised stranded child %s (%s)", child_id, evidence.get(
        "evidence", commit,
    ))
    return {"finalised": True, "commit": commit, "reason": reason}


def _stranded_children_comment(
    stranded: list[dict[str, Any]],
    parent_branch: str,
    finalised: list[dict[str, Any]] | None = None,
) -> str:
    """Build the one-per-drive worklog comment naming stranded children.

    Args:
        stranded: The ``stranded_children`` report entries.
        parent_branch: Branch the work was pushed to.
        finalised: Children automatically finalised in this drive invocation
            (``--finalise-stranded``); recorded in the same audit comment.

    Returns:
        Comment text naming each stranded child and how to finalise it.
    """
    finalised = finalised or []
    lines: list[str] = []
    if stranded:
        lines += [
            (
                f"Drive detected {len(stranded)} stranded child(ren): a session "
                f"failed after the work was pushed to `{parent_branch}`, so the "
                "item is still non-terminal and blocks `wl next`."
            ),
            "",
        ]
        for item in stranded:
            lines.append(
                f"- `{item.get('id')}` — status={item.get('status') or '?'}, "
                f"stage={item.get('stage') or '?'}, {item.get('evidence', '')}"
            )
        lines += [
            "",
            (
                "Finalise the item(s) once the pushed work is verified (or re-run "
                "drive with `--finalise-stranded` to let it finalise evidence-gated "
                "children automatically):"
            ),
            "",
        ]
        for item in stranded:
            lines.append(
                f"  wl update {item.get('id')} --status completed --stage in_review"
            )
    if finalised:
        if lines:
            lines.append("")
        lines.append(
            f"Automatically finalised {len(finalised)} stranded child(ren) "
            "(`--finalise-stranded`, evidence-gated: pushed commit verified, "
            "item still in-progress, worktree clean):"
        )
        lines.append("")
        for item in finalised:
            lines.append(
                f"- `{item.get('id')}` — completed/in_review, "
                f"commit {item.get('commit') or '?'}"
            )
    return "\n".join(lines)


def phase_drive(
    work_item_id: str,
    json_output: bool = False,
    no_refactor: bool = False,
    parent_branch: str = DEFAULT_PARENT_BRANCH,
    verbose: bool = False,
    max_child_sessions: int = DEFAULT_MAX_CHILD_SESSIONS,
    child_timeout: int | None = None,
    finish_attempt_limit: int | None = None,
    infra_retry_limit: int = DEFAULT_INFRA_RETRY_LIMIT,
    pane_sessions: bool | None = None,
    focus: bool = DEFAULT_PANE_FOCUS,
    delay: Callable[[float], None] | None = None,
    spawn_session: Callable[..., dict[str, Any]] | None = None,
    pane_client: Any | None = None,
    parent_runner: Callable[..., dict[str, Any] | None] | None = None,
    commit_evidence: Callable[[str], str | None] | None = None,
    finalise_stranded: bool = False,
    finalise_child: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Phase: drive every child of a parent to a terminal state in one call.

    Resolves SA-0MUWBHEEF002A6BO in line with the session-per-child rule
    (SA-0MTLCCIPU0050Q52): instead of requiring the caller to re-run
    ``parent`` and implement each child by hand, this phase loops over
    ``phase_parent`` reports and, for every startable child, spawns a
    **fresh Pi session** in that child's own worktree. Each session runs the
    standard leaf implement workflow, so context isolation, worktree
    isolation, dependency order and parent-advanced-last are all preserved.

    The loop:

    1. Run ``parent`` (subprocess-isolated): it finishes any changed child
       and starts the next startable one.
    2. If all children are terminal → parent advanced → done.
    3. Otherwise spawn a fresh session for ``next_child`` in its worktree
       and wait for it.
    4. Repeat. A child is retried at most ``max_child_sessions`` times so a
       session that never reaches a terminal state cannot loop forever.

    A parent report carrying the blocked verdict (``blocked: true`` /
    ``parent_blocked_by``) stops the chain **without** spawning a session: the
    invoked item is itself blocked by an unmet same-project outbound edge or a
    ``blocked`` status, so starting a child would override a producer's hold
    and edit files the pending dependency owns (SA-0MVS1QS9J009DGPT). This is
    a soft wait — the drive report forwards ``parent_blocked_by`` /
    ``parent_blocked_reason``, keeps ``success`` true and does **not** set
    ``failed_child`` — distinct from a hard per-child failure.

    A per-child session timeout (or non-zero exit) is treated as a *bound*,
    not a verdict: after a failed spawn the child is re-fetched and classified
    with :func:`_reconcile_spawn_failure`. A child that already reached a
    terminal state (``completed``/``done``/``in_review``) is recorded as
    ``completed-late`` and the chain continues; a genuinely incomplete child
    keeps the pre-existing hard failure (SA-0MVHXWLPG007OQVN).

    Failing ``finish`` executions are bounded too (SA-0MVLJXS7R000XIFL): a
    ``finishing_failed`` parent report is counted per child in
    ``finish_attempts`` (distinct from the session-spawn ``attempts``), and
    the loop re-runs ``parent`` — which retries the ``finish`` — only while
    the child's count stays below ``finish_attempt_limit``. Once the bound is
    exhausted the chain stops and the report carries the count, so the same
    failing ``finish`` (e.g. an abandoned dirty worktree whose build/tests
    are red) can never be executed unboundedly. The bound defaults to
    ``max_child_sessions``, so the default behaviour (one attempt) is
    unchanged from before this feature.

    Before the chain stops, the exhausted child is reconciled
    (SA-0MVLJXTYY005XQDV): it is re-fetched with ``wl show`` and classified
    with :func:`_is_terminal_child`. A child that reached a terminal state
    despite the failed finish is recorded as ``terminal-reconciled`` and the
    loop continues; only a genuinely non-terminal child keeps the hard
    failure. Terminal children are never finished, reset or re-spawned.

    A non-terminal child whose finish bound is exhausted is recovered instead
    of livelocking (SA-0MVLJXVND0015LPW): its **own** worktree is reset to the
    parent-branch HEAD (``git reset --hard <parent_branch>`` + ``git clean
    -fd``, isolation-gated so the main checkout can never be touched) and its
    status is set back to ``open`` through :class:`StatusLifecycle`, with an
    audit comment recording the failure reason, the finish-attempt count and
    the reset performed. The loop then continues, so the next iteration
    starts a fresh session for that child. The reset runs at most once per
    child per drive invocation: a second exhaustion after a successful reset
    stops the chain (bounded, truthful) rather than resetting forever.

    Args:
        work_item_id: The parent work item ID.
        json_output: If True, emit JSON.
        no_refactor: Passed through to ``parent``.
        parent_branch: Parent branch for child worktrees.
        verbose: Enable verbose logging (and child ``--verbose``).
        max_child_sessions: Max fresh sessions per child before stopping.
        child_timeout: Per-child session timeout in seconds (default from
            :func:`_resolve_child_session_timeout`).
        finish_attempt_limit: Max failing ``finish`` executions per child in
            one drive invocation (default: ``max_child_sessions``). A
            non-positive value is clamped to 1.
        infra_retry_limit: Max **infrastructure**-refusal retries per child in
            one drive invocation (default: ``DEFAULT_INFRA_RETRY_LIMIT``).
            These run on a budget separate from ``finish_attempt_limit``,
            with bounded exponential backoff, and never spawn a session — a
            refusal that clears within the budget lets the same ``finish``
            succeed and the chain continue (LP-0MVWEP292000ODBS). A
            non-positive value is clamped to 1.
        pane_sessions: Opt in to the Herdr pane-driven transport
            (``--pane-sessions`` / ``--no-pane-sessions``). ``None`` (the
            default) is env-driven via ``IMPLEMENT_DRIVE_PANE_SESSIONS``.
            Pane driving additionally requires Herdr to be detected
            (``HERDR_ENV=1`` and a resolvable herdr binary); otherwise the
            headless ``pi -p`` spawner is used unchanged
            (SA-0MVPHYIIE002OBC8).
        focus: Whether each new child pane takes focus (``--focus``).
            Default False so the caller's focus is never stolen.
        delay: Injectable backoff sleeper for infrastructure retries (tests
            pass a zero-cost recorder; default ``time.sleep``).
        spawn_session: Injectable spawner (tests); defaults to the transport
            chosen by :func:`_select_session_spawner`
            (:func:`_pane_session_spawner` when Herdr is detected and the
            operator opted in, else :func:`_default_session_spawner`).
        pane_client: Injectable ``HerdrSessionClient`` for the pane transport
            (tests); forwarded to :func:`_pane_session_spawner`.
        parent_runner: Injectable ``parent`` runner (tests); defaults to
            :func:`_invoke_implement`.
        commit_evidence: Injectable pushed-commit evidence provider (tests)
            used by stranded-child detection; defaults to
            :func:`_git_commit_evidence`.
        finalise_stranded: When True, a stranded child that passes every
            safety gate (verified pushed commit, still in-progress, not
            claimed by another agent, worktree clean) is finalised to
            ``completed``/``in_review`` and the chain continues. Default
            False — detection is read-only (SA-0MVLJWTGP005XJPP).
        finalise_child: Injectable finalisation path (tests); defaults to
            :func:`_finalise_stranded_child`.

    Returns:
        Report dict with per-child results, sessions spawned, whether the
        parent was advanced, and ``completed_late`` — the number of children
        whose session timed out after they had already reached a terminal
        state. Spawn-failure entries additionally carry ``timed_out``,
        ``timeout_seconds``, ``terminal``, ``terminal_status`` and
        ``terminal_stage`` so the two timeout outcomes are distinguishable
        (SA-0MVLJWOJ8004ABBA). A non-terminal child whose work is already on
        the parent branch additionally appears in ``stranded_children`` with
        its pushed-commit evidence (SA-0MVLJWRVO000QQNP). ``finish_attempts``
        maps each child whose ``finish`` failed to the number of failing
        finish executions observed, and ``finish_attempt_limit`` reports the
        bound applied (SA-0MVLJXS7R000XIFL). ``infra_retries`` maps each
        child whose ``finish`` was refused for an *infrastructure* reason to
        the number of retries consumed on the separate infra budget, and
        ``infra_retry_limit`` reports that bound (LP-0MVWEP292000ODBS); each
        retry also appears in ``children_driven`` as ``"infra-retry"`` with
        the failing ``step``/``reason`` and the attempt number.
        ``terminal_reconciled`` counts
        children recorded as done after their finish failed
        (SA-0MVLJXTYY005XQDV); ``reset_for_retry`` counts non-terminal
        children whose worktree was reset for a fresh session
        (SA-0MVLJXVND0015LPW); ``preserved_refs`` lists the children whose
        reset was **refused** because their worktree carries commits, each as
        ``{id, ref, commit}`` with the tip parked on ``refs/wip/<id>``
        (parent AC3, LP-0MVWEP2KB002WMRY). A child whose per-work-item lock is held by
        another live session is reported with ``action: "skipped-held"``,
        ``skipped_held: True``, ``held_by`` and ``held_children`` — and no
        session is spawned for it (SA-0MVQ5AXLF002PAQ3). Worktree removal is
        driver-owned: each driven child's ``finish`` defers it, so a terminal
        child's clean tree is removed once its session exits and the entry
        records ``worktree_removed`` plus its ``worktree_cleanup`` outcome;
        ``retained_worktrees`` lists every tree that was kept (with a
        remedy) and ``worktree_sweep`` records the start-of-run sweep of
        terminal children's leftover trees (SA-0MVTE8XAJ002IUDR). Every
        ``children_driven`` entry additionally carries ``transport`` and
        ``pane_id`` (``None`` for the headless transport); a child whose pane
        was closed once its terminal stage was confirmed records
        ``pane_closed``/``pane_cleanup``, while a pane kept open is named in
        ``retained_panes`` with its verdict and inspection remedy. A child
        whose driven agent reported ``blocked`` sets ``blocked_child`` /
        ``blocked_pane``, stops the chain and leaves its pane open
        (SA-0MVPHZ1CI0045QR0 AC2/AC3/AC4/AC5).
    """
    report: dict[str, Any] = {
        "phase": "drive",
        "work_item_id": work_item_id,
        "success": False,
        "parent_advanced": False,
        "sessions_spawned": 0,
        "children_driven": [],
        "completed_late": 0,
        "terminal_reconciled": 0,
        "reset_for_retry": 0,
        "preserved_refs": [],
        "finish_attempts": {},
        "finish_attempt_limit": None,
        "infra_retries": {},
        "infra_retry_limit": None,
        "message": "",
    }

    # ── Recursion guard: a driven child session must not drive again ──
    if os.environ.get(DRIVE_RECURSION_ENV) == "1":
        msg = (
            "Refusing to drive from within a driven child session "
            f"({DRIVE_RECURSION_ENV}=1). Driven sessions implement their own "
            "work item with the leaf workflow and must not re-enter drive."
        )
        LOG.error(msg)
        report["message"] = msg
        report["recursion_blocked"] = True
        _emit_phase_report(report, json_output)
        return report

    # ── Validate + fetch children ───────────────────────────────────
    if not WORK_ITEM_ID_PATTERN.match(work_item_id):
        report["message"] = (
            f"Invalid work item ID format: {work_item_id}. "
            "Expected pattern like SA-XXXXXXXXXXX"
        )
        _emit_phase_report(report, json_output)
        return report

    parent = wl_show(work_item_id)
    if not parent:
        report["message"] = (
            f"Work item {work_item_id} not found or failed to fetch"
        )
        _emit_phase_report(report, json_output)
        return report

    children = wl_show_children(work_item_id)
    if not children:
        report["leaf"] = True
        report["message"] = (
            f"Work item {work_item_id} has no children — behave as a leaf "
            "item: use `implement.py start/finish` as usual (nothing to drive)."
        )
        _emit_phase_report(report, json_output)
        return report

    if parent_runner is None:
        def runner(action, wid, no_refactor=False, verbose=False):
            # parse_on_failure surfaces the worktree-placement guard's
            # actionable failure (parsed from --json stdout) instead of the
            # generic "parent failed" message.
            return _invoke_implement(
                action, wid, no_refactor=no_refactor, verbose=verbose,
                parse_on_failure=True,
            )
    else:
        runner = parent_runner
    spawner = spawn_session or _select_session_spawner(
        pane_sessions=pane_sessions,
        focus=focus,
        env=os.environ,
        client=pane_client,
    )
    timeout = child_timeout or _resolve_child_session_timeout()
    max_child_sessions = max(1, int(max_child_sessions))
    finish_attempt_limit = (
        max_child_sessions if finish_attempt_limit is None
        else max(1, int(finish_attempt_limit))
    )
    report["finish_attempt_limit"] = finish_attempt_limit
    # Clamped to at least 1 so a non-positive --infra-retry-limit can never
    # open an unbounded retry loop (LP-0MVWEP292000ODBS AC1/AC5).
    infra_retry_limit = max(1, int(infra_retry_limit))
    report["infra_retry_limit"] = infra_retry_limit
    # Injectable backoff sleeper: tests substitute a zero-delay recorder so
    # the retry schedule is exercised without sleeping (LP-0MVWEP292000ODBS
    # AC6).
    sleeper = delay or time.sleep

    attempts: dict[str, int] = {}
    finish_attempts: dict[str, int] = {}
    infra_retries: dict[str, int] = {}
    reset_cids: set[str] = set()
    # The report's own list, so entries recorded mid-loop (worktree cleanup
    # outcomes) are visible to the resolvers and to the final report.
    driven: list[dict[str, Any]] = report["children_driven"]
    #: Child worktrees awaiting a terminal verdict from the next
    #: ``phase_parent`` report (the success path performs no extra store
    #: fetch — SA-0MVHXWLPG007OQVN risk R1).
    pending_cleanup: dict[str, str] = {}
    #: Panes created by this drive whose child is still awaiting the parent's
    #: terminality verdict; resolved to a close (terminal) or a reported
    #: retention (still non-terminal) by the next parent report
    #: (SA-0MVPHZ1CI0045QR0 AC3).
    pending_panes: dict[str, str] = {}
    stranded_comment_posted = False
    finaliser = finalise_child or _finalise_stranded_child
    # Hard bound on the loop: each child may consume at most
    # max_child_sessions spawns plus finish_attempt_limit failing finish
    # retries plus infra_retry_limit infrastructure retries plus one
    # terminal classification, and the final iteration advances the parent.
    # The infra budget is included so a chain that needs many infra retries
    # before its session can never trip the bound prematurely
    # (LP-0MVWEP292000ODBS AC7).
    max_iterations = (
        len(children)
        * (max_child_sessions + finish_attempt_limit + infra_retry_limit + 1)
    ) + 1

    # ── Start-of-run sweep: terminal children's leftover worktrees ──
    # A driven child's ``finish`` defers worktree removal (the session owns
    # its cwd), so the driver is its owner. A drive interrupted between a
    # session's exit and this removal would strand the tree forever; sweeping
    # leftover trees of *terminal* children at the start keeps the deferral
    # from leaking (SA-0MVTE8XAJ002IUDR AC7). Bounded, clean trees only,
    # never fatal.
    _sweep_terminal_child_worktrees(children, report)

    for _ in range(max_iterations):
        parent_report = runner(
            "parent", work_item_id, no_refactor=no_refactor, verbose=verbose,
        )
        if parent_report is None:
            report["message"] = (
                f"`implement.py parent {work_item_id}` failed (see logs)."
            )
            break

        # Resolve queued worktree removals from this report's per-child
        # statuses before acting on anything else (SA-0MVTE8XAJ002IUDR AC5).
        _resolve_pending_worktree_cleanups(
            parent_report, pending_cleanup, report
        )
        # Same signal for panes: a child the parent now reports terminal has
        # its pane closed, a still-non-terminal child keeps its pane open and
        # is reported as retained (SA-0MVPHZ1CI0045QR0 AC3).
        _resolve_pending_pane_closes(
            parent_report, pending_panes, report, pane_client
        )

        if parent_report.get("parent_advanced"):
            report["parent_advanced"] = True
            report["success"] = True
            report["message"] = parent_report.get(
                "message",
                f"All children of {work_item_id} are terminal; parent advanced.",
            )
            if report["completed_late"]:
                report["message"] = (
                    f"{report['message']} "
                    f"{report['completed_late']} child(ren) completed late "
                    f"(session timed out but the child had already reached a "
                    f"terminal state)."
                )
            break

        if parent_report.get("blocked"):
            # The invoked item is blocked by its OWN outbound dependency edge
            # and/or its own ``blocked`` status (SA-0MVS1QS9J009DGPT). This is
            # a legitimate producer-mandated wait, not a failure: forward the
            # verdict (with the blocker ids/statuses) and spawn NOTHING — a
            # child session would create a worktree and edit files the
            # still-pending dependency owns.
            report["blocked"] = True
            report["success"] = True
            report["parent_advanced"] = False
            report["parent_blocked_by"] = parent_report.get(
                "parent_blocked_by", []
            )
            report["parent_blocked_reason"] = parent_report.get(
                "parent_blocked_reason", PARENT_BLOCKED_REASON_DEPENDENCY
            )
            if parent_report.get("coordination_blockers"):
                report["coordination_blockers"] = parent_report[
                    "coordination_blockers"
                ]
            report["children_processed"] = parent_report.get(
                "children_processed", []
            )
            report["message"] = parent_report.get(
                "message",
                f"{work_item_id} is blocked; no child session was spawned.",
            )
            break

        if parent_report.get("success") is False:
            failed_cid = parent_report.get("next_child")
            if parent_report.get("finishing_failed") and failed_cid:
                # Classify the finish failure BEFORE the finish-attempt
                # accounting: an *infrastructure* refusal (local_dev_diverged,
                # a held main-checkout lock, a dropped push transport) is
                # retried on its own bounded budget with backoff — it never
                # consumes the finish-attempt budget and never spawns a
                # session — so a refusal that clears seconds later no longer
                # discards a committed slice (LP-0MVWEP292000ODBS, parent
                # epic AC2).
                finish_failure = parent_report.get("finish_failure") or {}
                failure_kind = finish_failure.get("kind")
                if (
                    failure_kind == "infrastructure"
                    and infra_retries.get(failed_cid, 0) < infra_retry_limit
                ):
                    infra_retries[failed_cid] = (
                        infra_retries.get(failed_cid, 0) + 1
                    )
                    attempt = infra_retries[failed_cid]
                    driven.append({
                        "id": failed_cid,
                        "result": "infra-retry",
                        "step": finish_failure.get("step"),
                        "reason": finish_failure.get("reason"),
                        "attempt": attempt,
                        "message": _describe_finish_failure(
                            finish_failure
                        ),
                    })
                    backoff = _drive_infra_backoff_delay(attempt)
                    LOG.warning(
                        "Child %s finish refused for an infrastructure "
                        "reason (step=%r, reason=%r) — infra-retry %d/%d "
                        "in %ss: re-running `parent` without spawning a "
                        "session.",
                        failed_cid,
                        finish_failure.get("step"),
                        finish_failure.get("reason"),
                        attempt,
                        infra_retry_limit,
                        backoff,
                    )
                    # The retry verdict is durable on the child: the comment
                    # names the real failing step/reason so a producer can
                    # tell an infrastructure refusal from a gate failure
                    # without reading the child's log (parent AC5,
                    # LP-0MVWEP37K000JU0O AC4).
                    desc = _describe_finish_failure(finish_failure)
                    wl_add_comment(
                        failed_cid,
                        "Drive retried this child's `finish` after a "
                        "transient infrastructure refusal"
                        + (f" ({desc})" if desc else "")
                        + f". Infra-retry {attempt}/{infra_retry_limit} in "
                        f"{backoff}s; no session was spawned — the loop "
                        "re-runs `parent` to retry `finish`.",
                    )
                    sleeper(backoff)
                    continue
                if failure_kind == "infrastructure":
                    # The infra budget is spent: annotate the parent message
                    # so the stopping verdict names the real failing
                    # step/reason, then fall through to the existing bounded
                    # finish-attempt path (LP-0MVWEP292000ODBS AC5).
                    parent_report = {
                        **parent_report,
                        "message": (
                            f"{parent_report.get('message', '')} "
                            "(infrastructure refusal at step "
                            f"{finish_failure.get('step')!r}, reason "
                            f"{finish_failure.get('reason')!r} exhausted "
                            f"the infra-retry budget of {infra_retry_limit})"
                        ),
                    }
                # Bound the retries of a failing `finish`: every re-run of
                # `parent` re-executes the same finish for that child, so the
                # count is the number of real finish executions observed
                # (SA-0MVLJXS7R000XIFL).
                finish_attempts[failed_cid] = (
                    finish_attempts.get(failed_cid, 0) + 1
                )
                if finish_attempts[failed_cid] < finish_attempt_limit:
                    LOG.warning(
                        "Child %s finish failed (attempt %d/%d) — bounded "
                        "retry: re-running `parent` so finish is attempted "
                        "again.",
                        failed_cid,
                        finish_attempts[failed_cid],
                        finish_attempt_limit,
                    )
                    continue
                LOG.warning(
                    "Child %s finish failed %d time(s), exhausting the "
                    "finish-attempt bound (%d); reconciling the child's "
                    "actual state.",
                    failed_cid,
                    finish_attempts[failed_cid],
                    finish_attempt_limit,
                )
                # Bound exhausted: re-fetch the child and reconcile. A
                # child whose finish failed but which nevertheless reached a
                # terminal state (e.g. the push succeeded and a later gate
                # step failed) is done, not failed — recording it as
                # terminal lets the epic advance
                # (SA-0MVLJXTYY005XQDV). This fetch runs only on the slow
                # finish-failure path.
                reconciliation = _reconcile_finish_failure(
                    failed_cid,
                    parent_report,
                    finish_attempts[failed_cid],
                )
                if reconciliation["terminal"]:
                    driven.append({
                        "id": failed_cid,
                        "result": "terminal-reconciled",
                        "reconciled": True,
                        "status": reconciliation["status"],
                        "stage": reconciliation["stage"],
                        "finish_attempts": finish_attempts[failed_cid],
                        "evidence": reconciliation["evidence"],
                        "reason": reconciliation["reason"],
                        "message": _describe_finish_failure(
                            finish_failure
                        ),
                    })
                    report["terminal_reconciled"] = (
                        report.get("terminal_reconciled", 0) + 1
                    )
                    desc = _describe_finish_failure(finish_failure)
                    suffix = (
                        f" Finish evidence: {desc}." if desc else ""
                    )
                    wl_add_comment(
                        failed_cid,
                        "Drive reconciled this child after its `finish` "
                        f"failed {finish_attempts[failed_cid]} time(s): "
                        f"{reconciliation['evidence']}.{suffix} Recorded as "
                        "done (`terminal-reconciled`); no reset was "
                        "performed.",
                    )
                    LOG.info(
                        "Reconciled %s after a failed finish: terminal "
                        "(%s); continuing the drive loop.",
                        failed_cid,
                        reconciliation["evidence"],
                    )
                    continue
                # Non-terminal: break the livelock by resetting the child to a
                # clean startable state and letting the loop continue so the
                # next iteration starts a fresh session
                # (SA-0MVLJXVND0015LPW). The reset is bounded to once per
                # child per drive invocation: a second exhaustion after the
                # reset stops the chain instead of resetting forever.
                if failed_cid in reset_cids:
                    LOG.error(
                        "Child %s finish failed again after its worktree "
                        "was already reset in this drive invocation; "
                        "stopping the drive chain.",
                        failed_cid,
                    )
                    report["message"] = (
                        f"{parent_report.get('message', f'Failed to finish child {failed_cid}')} "
                        f"(finish-attempt bound {finish_attempt_limit} "
                        f"exhausted again after a reset; child is still "
                        f"non-terminal: "
                        f"{reconciliation['evidence'] or 'state unreadable'}"
                        f"{_finish_evidence_suffix(finish_failure)})."
                    )
                    report["failed_child"] = failed_cid
                    break
                reset_worktree = (
                    parent_report.get("worktree_path")
                    or _discover_worktree(failed_cid)
                    or ""
                )
                reset_result = _reset_child_worktree(
                    reset_worktree, parent_branch, child_id=failed_cid,
                )
                if reset_result.get("preserved_ref"):
                    # The worktree carries commits the parent branch does not
                    # have: the reset was refused and the tip parked on
                    # refs/wip/<child-id> BEFORE any destructive command ran
                    # (parent AC3, LP-0MVWEP2KB002WMRY). Record the
                    # preservation, comment it on the child and stop the
                    # chain — spawning a session would race work that was
                    # deliberately preserved.
                    preserved_entry = {
                        "id": failed_cid,
                        "ref": reset_result["preserved_ref"],
                        "commit": reset_result["preserved_commit"],
                    }
                    report.setdefault("preserved_refs", []).append(
                        preserved_entry
                    )
                    wl_add_comment(
                        failed_cid,
                        _preserved_ref_comment(
                            failed_cid, preserved_entry, reconciliation,
                            finish_failure,
                        ),
                    )
                    LOG.error(
                        "Refusing to reset child %s: it carries commits. "
                        "Tip preserved at %s (%s).",
                        failed_cid,
                        preserved_entry["ref"],
                        preserved_entry["commit"],
                    )
                    report["message"] = (
                        f"{parent_report.get('message', f'Failed to finish child {failed_cid}')} "
                        f"(finish-attempt bound {finish_attempt_limit} "
                        f"exhausted; child is still non-terminal: "
                        f"{reconciliation['evidence'] or 'state unreadable'}; "
                        f"reset refused because the worktree carries "
                        f"commits — tip preserved at "
                        f"{preserved_entry['ref']} "
                        f"({preserved_entry['commit']})"
                        f"{_finish_evidence_suffix(finish_failure)})."
                    )
                    report["failed_child"] = failed_cid
                    break
                if not reset_result["success"]:
                    LOG.error(
                        "Refusing to reset child %s: %s",
                        failed_cid, reset_result["reason"],
                    )
                    report["message"] = (
                        f"{parent_report.get('message', f'Failed to finish child {failed_cid}')} "
                        f"(finish-attempt bound {finish_attempt_limit} "
                        f"exhausted; child is still non-terminal: "
                        f"{reconciliation['evidence'] or 'state unreadable'}; "
                        f"recovery reset refused: {reset_result['reason']}"
                        f"{_finish_evidence_suffix(finish_failure)})."
                    )
                    report["failed_child"] = failed_cid
                    break
                # Invariant guard (parent AC4, LP-0MVWEP2VQ001NPRK):
                # reset-for-retry is only for a worktree that carried **no**
                # commits ahead of the parent branch. The preservation gate
                # above refuses a commit-carrying worktree, so reaching this
                # point with commits would mean a committed slice was
                # discarded — fail closed instead of recording a
                # reset-for-retry that implies an uncommitted-only reset.
                violation = _reset_for_retry_violation(reset_result)
                if violation:
                    LOG.error(
                        "Refusing to record a reset-for-retry for child "
                        "%s: %s",
                        failed_cid, violation,
                    )
                    report["message"] = (
                        f"{parent_report.get('message', f'Failed to finish child {failed_cid}')} "
                        f"(finish-attempt bound {finish_attempt_limit} "
                        f"exhausted; child is still non-terminal: "
                        f"{reconciliation['evidence'] or 'state unreadable'}; "
                        f"reset-for-retry refused: {violation}"
                        f"{_finish_evidence_suffix(finish_failure)})."
                    )
                    report["failed_child"] = failed_cid
                    break
                reset_cids.add(failed_cid)
                # Record the discard *before* changing the status, so the
                # child owner's WIP is never discarded silently.
                wl_add_comment(
                    failed_cid,
                    _reset_for_retry_comment(
                        failed_cid,
                        finish_attempts[failed_cid],
                        str(parent_report.get("message", "") or ""),
                        reconciliation,
                        reset_result,
                        finish_failure,
                    ),
                )
                try:
                    # Durable-write reset after a failed finish: release the
                    # child's claim so a fresh drive session can re-dispatch it.
                    StatusLifecycle.update_status(
                        failed_cid, "open", assignee="",
                    )
                except RuntimeError as exc:
                    LOG.error(
                        "Failed to reset child %s to open after its "
                        "worktree reset: %s", failed_cid, exc,
                    )
                    report["message"] = (
                        f"Child {failed_cid} worktree was reset to "
                        f"{reset_result['ref']} but its status could not be "
                        f"set to open ({exc}); stopping the drive chain"
                        f"{_finish_evidence_suffix(finish_failure)}."
                    )
                    report["failed_child"] = failed_cid
                    break
                driven.append({
                    "id": failed_cid,
                    "result": "reset-for-retry",
                    "reset": True,
                    "status": "open",
                    "stage": reconciliation["stage"],
                    "finish_attempts": finish_attempts[failed_cid],
                    "discarded_paths": list(
                        reset_result.get("discarded_paths") or []
                    ),
                    "evidence": reconciliation["evidence"],
                    "message": _describe_finish_failure(finish_failure),
                    "reason": (
                        f"finish failed {finish_attempts[failed_cid]} "
                        f"time(s); worktree reset to {reset_result['ref']} "
                        "and the child queued for a fresh session."
                    ),
                })
                report["reset_for_retry"] = (
                    report.get("reset_for_retry", 0) + 1
                )
                LOG.info(
                    "Reset child %s worktree to %s after %d failed finish "
                    "attempt(s); continuing the drive loop with a fresh "
                    "session.",
                    failed_cid,
                    reset_result["ref"],
                    finish_attempts[failed_cid],
                )
                continue
            report["message"] = parent_report.get(
                "message",
                f"Phase parent reported a failure for {work_item_id}.",
            )
            report["failed_child"] = parent_report.get("next_child")
            break

        if parent_report.get("skipped_held"):
            # The parent stopped cleanly because a live holder owns the next
            # child's critical section. Surface the held classification and
            # its holder, and spawn nothing (AC3): a second session would race
            # the holder's worktree writes (SA-0MVOABYW00004978).
            report["action"] = ACTION_SKIPPED_HELD
            report["skipped_held"] = True
            report["held_by"] = parent_report.get("held_by")
            report["held_children"] = parent_report.get("held_children", [])
            report["children_processed"] = parent_report.get(
                "children_processed", []
            )
            report["message"] = parent_report.get(
                "message",
                f"A child of {work_item_id} is held by another "
                "implementation session; no session was spawned.",
            )
            break

        # A stale-lock reclamation recorded while the parent phase started the
        # child is forwarded into the drive report, so a ``drive --json``
        # consumer sees it without scraping the child's log
        # (SA-0MVUX6Q4S006KII3, parent AC3).
        _forward_child_lock_reclamation(
            report, str(parent_report.get("next_child") or ""), parent_report
        )

        child_id = parent_report.get("next_child")
        if not child_id:
            report["message"] = parent_report.get(
                "message",
                "No startable child: remaining children are blocked or held "
                "by another agent.",
            )
            if parent_report.get("blocked_children"):
                report["blocked_children"] = parent_report["blocked_children"]
            break

        worktree_path = (
            parent_report.get("worktree_path")
            or _discover_worktree(child_id)
            or ""
        )
        if not worktree_path:
            report["message"] = (
                f"No worktree path available for child {child_id}; cannot "
                "spawn its implementation session."
            )
            report["failed_child"] = child_id
            break

        attempts[child_id] = attempts.get(child_id, 0) + 1
        if attempts[child_id] > max_child_sessions:
            report["message"] = (
                f"Child {child_id} did not reach a terminal state after "
                f"{max_child_sessions} session(s); stopping to avoid a loop."
            )
            report["failed_child"] = child_id
            break

        env = _child_session_env(worktree_path=worktree_path)
        LOG.info(
            "Driving child %s (session %d/%d) in %s",
            child_id, attempts[child_id], max_child_sessions, worktree_path,
        )
        spawn_result = spawner(child_id, worktree_path, timeout, env)
        report["sessions_spawned"] += 1
        entry = {
            "id": child_id,
            "worktree_path": worktree_path,
            "attempt": attempts[child_id],
            "result": "finished" if spawn_result.get("success") else "failed",
            "reason": spawn_result.get("reason", ""),
            # Report parity (AC5): every driven child names its transport and
            # the pane it ran in (``None`` for the headless transport).
            **_driven_pane_fields(spawn_result),
        }
        driven.append(entry)
        pane_id = entry.get("pane_id")

        if (
            spawn_result.get("success")
            and spawn_result.get("agent_status") == "blocked"
        ):
            # The driven agent settled in ``blocked``: it needs a human
            # decision, so the drive stops instead of re-spawning a session
            # that would immediately block again. The pane is deliberately
            # LEFT OPEN and named, never silently closed or abandoned
            # (AC2).
            entry["result"] = "blocked"
            entry["agent_status"] = "blocked"
            report["blocked_child"] = child_id
            report["blocked_pane"] = pane_id
            if pane_id:
                _retain_driven_pane(
                    child_id,
                    pane_id,
                    PANE_VERDICT_BLOCKED,
                    entry,
                    report,
                    reason=(
                        "the driven agent reported `blocked`; a human "
                        "decision is needed"
                    ),
                )
            inspect = (
                f"its pane {pane_id} was left open for inspection "
                f"(`{_pane_inspect_remedy(pane_id)}`)"
                if pane_id
                else "no pane is attached (headless transport)"
            )
            report["message"] = (
                f"Child {child_id} reported `blocked`; the drive chain "
                f"stopped — {inspect}. Resolve the blocker, then re-run drive."
            )
            wl_add_comment(
                child_id,
                f"Drive stopped: the driven session for this child reported "
                f"`blocked` (a human decision is needed). The drive chain "
                f"stopped and {inspect}. Resolve the blocker and re-run "
                f"`implement.py drive`.",
            )
            LOG.warning(
                "Child %s reported blocked; stopping the drive chain (pane=%s)",
                child_id, pane_id,
            )
            break

        if spawn_result.get("success"):
            # Queue the tree for removal: the follow-up `parent` report names
            # the child's terminal state, which is the signal to remove it
            # without an extra store fetch on the hot path.
            pending_cleanup[child_id] = worktree_path
            # The pane follows the same terminality signal: close on the
            # confirmed terminal stage, retain (and report) otherwise (AC3).
            if pane_id:
                pending_panes[child_id] = pane_id

        if not spawn_result.get("success"):
            # A failed spawn is a bound, not a verdict: reconcile against the
            # child's actual work-item state (SA-0MVHXWLPG007OQVN). A child
            # that committed, pushed and reached a terminal state before its
            # session was killed is *completed late*, not failed.
            reconciliation = _reconcile_spawn_failure(
                child_id, spawn_result, timeout,
            )
            entry["timed_out"] = reconciliation["timed_out"]
            entry["timeout_seconds"] = reconciliation["timeout_seconds"]
            entry["terminal"] = reconciliation["terminal"]
            entry["terminal_status"] = reconciliation["status"]
            entry["terminal_stage"] = reconciliation["stage"]
            entry["reason"] = reconciliation["reason"]
            if reconciliation["terminal"]:
                entry["result"] = "completed-late"
                entry["reconciled"] = True
                report["completed_late"] += 1
                LOG.info(
                    "Child %s completed late (%s); continuing the drive loop.",
                    child_id, reconciliation["evidence"],
                )
                # Terminality is confirmed, so the pane this session ran in is
                # no longer needed: close it (fail-open — a failed close is
                # retained and reported, never leaked) before the deferred
                # worktree removal (AC3/AC4).
                if pane_id:
                    _close_driven_pane(
                        child_id, pane_id, entry, report, pane_client,
                        reason="child completed late (terminal confirmed)",
                    )
                # This is the exact path the production incident took: the
                # session was killed after its push, so the deferred worktree
                # must be released here too — a hook on the success path only
                # would leak it. The reconciliation already established
                # terminality, so removal is immediate (no extra fetch).
                _maybe_remove_child_worktree(
                    child_id, worktree_path, entry, report
                )
                continue

            # Not terminal: dispose of the pane this session created. A
            # timeout means the wait expired and the session is gone, so its
            # pane is closed (AC4: the timeout bound closes the pane and stops
            # the chain). Any other failure keeps the pane open as debugging
            # evidence and titles it with the ⚠️ verdict (AC3).
            if pane_id:
                if reconciliation["timed_out"]:
                    _close_driven_pane(
                        child_id, pane_id, entry, report, pane_client,
                        reason=(
                            "session timed out after "
                            f"{reconciliation['timeout_seconds']}s"
                        ),
                    )
                else:
                    _retain_driven_pane(
                        child_id,
                        pane_id,
                        PANE_VERDICT_FAILED,
                        entry,
                        report,
                        reason=(
                            "child session failed and the child is not "
                            "terminal; pane kept open for inspection"
                        ),
                    )

            # Not terminal: the hard failure stands. The killed session could
            # not release the claim it held (a timeout kill is a SIGKILL, so
            # no exit hook runs), so the surviving driver releases it with a
            # durable write before any further diagnosis: an in-progress
            # child would otherwise stay on a scheduling hold and be
            # invisible to ``wl next`` (SA-0MVVF75ZA007XNKB). This also lets
            # the evidence-gated finaliser below run — it refuses a child
            # still carrying someone's assignee. The release never masks the
            # failure: the chain still stops with ``failed_child``.
            claim_release = _release_orphaned_child_claim(child_id)
            entry["claim_release"] = claim_release
            report["claim_release"] = claim_release
            if claim_release.get("released"):
                LOG.info(
                    "Released the orphaned claim on %s after its child "
                    "session failed: %s",
                    child_id, claim_release["reason"],
                )

            # Before stopping the chain, check whether the child is
            # *stranded* — its work is already on the parent branch but the
            # item never reached a terminal state (SA-0MVLJWRVO000QQNP).
            # Read-only: report + one parent comment, no status mutation.
            stranded = _detect_stranded_child(
                child_id, worktree_path, parent_branch,
                commit_evidence=commit_evidence,
            )
            if stranded["stranded"]:
                entry["stranded"] = True
                entry["commit_evidence"] = stranded["commit"]
                stranded_entry = {
                    "id": child_id,
                    "status": reconciliation["status"],
                    "stage": reconciliation["stage"],
                    "commit": stranded["commit"],
                    "evidence": stranded["evidence"],
                    "reason": stranded["reason"],
                }

                # Opt-in, evidence-gated finalisation. Read-only by default:
                # without --finalise-stranded the item is only diagnosed.
                if (
                    finalise_stranded
                    and not _worktree_has_uncommitted_changes(worktree_path)
                ):
                    outcome = finaliser(child_id, stranded)
                    if outcome.get("finalised"):
                        entry["result"] = "finalised"
                        entry["finalised"] = True
                        report.setdefault("finalised_children", []).append({
                            "id": child_id,
                            "status": "completed",
                            "stage": "in_review",
                            "commit": outcome.get("commit", stranded["commit"]),
                            "reason": outcome.get("reason", ""),
                        })
                        if not stranded_comment_posted:
                            wl_add_comment(
                                work_item_id,
                                _stranded_children_comment(
                                    report.get("stranded_children", []),
                                    parent_branch,
                                    finalised=report["finalised_children"],
                                ),
                            )
                            stranded_comment_posted = True
                        LOG.info(
                            "Finalised stranded child %s; continuing the "
                            "drive loop.",
                            child_id,
                        )
                        continue
                    LOG.warning(
                        "Refused to finalise stranded child %s: %s",
                        child_id, outcome.get("reason", "unknown reason"),
                    )
                    stranded_entry["finalise_refused"] = outcome.get(
                        "reason", "unknown reason",
                    )

                report.setdefault("stranded_children", []).append(
                    stranded_entry,
                )
                if not stranded_comment_posted:
                    wl_add_comment(
                        work_item_id,
                        _stranded_children_comment(
                            report["stranded_children"], parent_branch,
                        ),
                    )
                    stranded_comment_posted = True
                LOG.warning(
                    "Child %s is stranded: %s",
                    child_id, stranded["reason"],
                )
                report["message"] = (
                    f"Child session for {child_id} failed and the child is "
                    f"stranded (not terminal, but {stranded['evidence']}); "
                    f"finalise it or re-run drive with --finalise-stranded. "
                    f"Reason: {spawn_result.get('reason', 'unknown error')}"
                )
                report["failed_child"] = child_id
                break

            report["message"] = (
                f"Child session for {child_id} failed: "
                f"{spawn_result.get('reason', 'unknown error')}"
            )
            report["failed_child"] = child_id
            break
    else:
        report["message"] = (
            f"Drive loop for {work_item_id} exceeded its iteration bound "
            f"({max_iterations}) without completing the epic."
        )

    report["children_driven"] = driven
    report["finish_attempts"] = dict(finish_attempts)
    report["infra_retries"] = dict(infra_retries)
    # A drive that ended before the follow-up parent report resolved a child's
    # queued worktree never strands it silently (SA-0MVTE8XAJ002IUDR AC7).
    _retain_pending_worktree_cleanups(pending_cleanup, report)
    # Panes are never leaked either: anything the terminality signal did not
    # resolve is reported as retained with its inspection remedy
    # (SA-0MVPHZ1CI0045QR0 AC3).
    _retain_pending_panes(pending_panes, report)
    _emit_phase_report(report, json_output)
    return report


def _is_worktree(path: Path) -> bool:
    """Check if *path* is a git worktree (not the main working tree).

    A git worktree has ``.git`` as a **file** containing a gitdir reference.
    The main working tree has ``.git`` as a **directory**.

    Args:
        path: The path to check.

    Returns:
        True if ``.git`` exists and is a file (worktree).
        False if ``.git`` is a directory (main working tree) or does not exist.
    """
    git_path = path / ".git"
    return git_path.is_file()


def _worktree_placement_violation(
    worktree_path: str,
    cwd: str | None = None,
    repo_root: str | None = None,
    parent_branch: str = DEFAULT_PARENT_BRANCH,
) -> str | None:
    """Return an error message when implementation work landed outside the worktree.

    All implementation work must happen inside the worktree created by
    ``implement.py start``. A violation exists only when ALL of:

    1. the current directory is not the worktree (or a subdirectory of it),
    2. the main checkout holds uncommitted changes outside ``.worklog/``, and
    3. the worktree itself holds NO changes (no dirty files and no commits
       ahead of the parent branch) — i.e. the work was done in the main
       checkout, not the worktree.

    Condition 3 keeps the gate precise: pre-existing/unrelated dirt in the
    main checkout does not block a legitimate finish when the work actually
    lives in the worktree.

    Args:
        worktree_path: Absolute path to the worktree.
        cwd: Directory to treat as the current directory (defaults to
            ``os.getcwd()``).
        repo_root: Main repo root (defaults to discovery from *cwd*).
        parent_branch: Branch the worktree forked from (default: ``dev``).

    Returns:
        An actionable error message string if placement is violated,
        otherwise ``None``.
    """
    wt = Path(worktree_path).resolve()
    cur = Path(cwd or os.getcwd()).resolve()
    if cur == wt or wt in cur.parents:
        return None  # already inside the worktree

    root = Path(repo_root or _get_repo_root(cur) or cur).resolve()
    root_expected = expected_dirty_paths(str(root))
    if not git_has_dirty_files(
        git_status(cwd=str(root)), expected_dirty=root_expected
    ):
        return None  # main checkout clean; work must be in the worktree

    if _git_path_has_changes(
        wt, parent_branch, expected_dirty=expected_dirty_paths(str(wt))
    ):
        return None  # work lives in the worktree; main-checkout dirt is unrelated

    return (
        f"Work done outside the worktree: uncommitted changes were found in "
        f"the main checkout at {root} while the worktree {wt} contains no "
        f"changes. All implementation work MUST be done in the worktree "
        f"created by `implement.py start`. Move your changes into the "
        f"worktree and re-run `implement.py finish`."
    )


def _git_path_has_changes(
    path: Path,
    parent_branch: str,
    expected_dirty: tuple[str, ...] = (),
) -> bool:
    """True when the git directory at *path* contains implementation changes.

    Detects uncommitted changes and commits **ahead of** *parent_branch*. A
    worktree that is merely *behind* the parent branch (HEAD differs from it,
    but zero commits ahead and no dirty files) holds no work and is reported
    as clean, so a long-running child is never handed to ``finish`` just
    because unrelated commits landed on the parent branch
    (SA-0MVRN5Q67005H3M5).

    Args:
        path: Directory to check (e.g. the worktree root).
        parent_branch: Branch the directory forked from (e.g. ``dev``).
        expected_dirty: Project-declared path prefixes to exclude.

    Returns:
        True if the directory is dirty or has commits ahead of
        *parent_branch*; False when it is clean and has none.
    """
    if git_has_dirty_files(
        git_status(cwd=str(path)), expected_dirty=expected_dirty
    ):
        return True
    ahead = run_cmd(
        ["git", "rev-list", "--count", f"{parent_branch}..HEAD"],
        cwd=str(path),
        check=False,
        capture=True,
    )
    if ahead.returncode == 0:
        try:
            return int(ahead.stdout.strip()) > 0
        except ValueError:
            return True  # unparseable count; fail open
    return True  # cannot compare (missing ref, git error); fail open


def _child_main_checkout_violation(
    child_id: str,
    worktree_path: str,
    parent_branch: str = DEFAULT_PARENT_BRANCH,
    repo_root: str | None = None,
) -> str | None:
    """Detect a child session that wrote to the main checkout, not its worktree.

    A driven child session receives ``cwd=<worktree>`` (and
    ``IMPLEMENT_WORKTREE_PATH``), but ``cwd`` is only a hint: an agent can
    prefix commands with ``cd <main-checkout> && ...`` and write there. This
    guard detects that failure after the fact so ``phase_parent``/``phase_drive``
    fail closed instead of silently proceeding. A violation exists only when
    ALL of:

    1. the child is non-terminal (the caller enforces this),
    2. the main checkout holds uncommitted changes outside ``.worklog/``, and
    3. the child's worktree holds NO changes (no dirty files and no commits
       ahead of the parent branch) — so the work did not land in the worktree.

    Condition 3 keeps the guard precise: pre-existing/unrelated dirt in the
    main checkout does not block a child whose work legitimately lives in its
    worktree. This mirrors :func:`_worktree_placement_violation`, minus the
    cwd-sensitive check that only applies to ``phase_finish``.

    Args:
        child_id: The child work item ID (named in the message).
        worktree_path: Absolute path to the child's worktree.
        parent_branch: Branch the worktree forked from (default: ``dev``).
        repo_root: Main checkout root (defaults to discovery from the
            worktree).

    Returns:
        An actionable error message naming the offending main-checkout paths,
        or ``None`` when there is no violation.
    """
    wt = Path(worktree_path)
    if not wt.exists():
        return None

    repo_root_str = repo_root or _get_repo_root(str(wt))
    if not repo_root_str:
        return None
    root = Path(repo_root_str).resolve()

    offending = _main_checkout_offending_paths(
        str(root), expected_dirty=expected_dirty_paths(str(root))
    )
    if not offending:
        return None  # main checkout clean (or only expected-dirty paths)

    if _git_path_has_changes(
        wt, parent_branch, expected_dirty=expected_dirty_paths(str(wt))
    ):
        return None  # work lives in the worktree; main-checkout dirt is unrelated

    listing = "\n".join(f"  - {p}" for p in offending[:20])
    if len(offending) > 20:
        listing += f"\n  … and {len(offending) - 20} more"
    return (
        f"Child {child_id} produced changes in the main checkout at {root} "
        f"instead of its worktree {wt}. The worktree has no changes (no "
        f"dirty files and no commits ahead of {parent_branch}), so the "
        f"child's work did not land in its own "
        f"worktree — `cwd` is not sufficient. Offending main-checkout paths:\n"
        f"{listing}\n"
        f"Move these changes into {wt} (or discard them), then re-run. A "
        f"driven child MUST run every write/edit with a path that resolves "
        f"inside its worktree."
    )


def _discover_worktree(work_item_id: str) -> str | None:
    """Discover the worktree path for a given work item.

    Checks:
    1. State file in a known worktree path
    2. Current directory (if inside a matching worktree)
    3. Scan .worklog/worktrees/ for matching directories

    Safety: will NOT return a path that looks like the main working tree
    (where ``.git`` is a directory).  This prevents catastrophic deletion
    when ``.implement_state.json`` is accidentally present in the repo root.

    Args:
        work_item_id: The work item ID.

    Returns:
        Absolute path to the worktree, or None if not found or if the
        candidate path is the main working tree (not a worktree).
    """
    # Check if state file is in the current directory or a parent
    cwd = Path.cwd().resolve()
    state_file = cwd / STATE_FILE_NAME
    if state_file.exists():
        # SAFETY: verify .git is a FILE (worktree), not a directory (main repo)
        if _is_worktree(cwd):
            return str(cwd)
        LOG.critical(
            "Refusing to treat %s as a worktree: .git is a directory, "
            "indicating the main working tree. .implement_state.json found "
            "in repo root is likely a stale artifact.",
            cwd,
        )
        return None

    # Check if we're inside a .worklog/worktrees/ directory
    if ".worklog/worktrees" in str(cwd):
        # SAFETY: also verify .git is a file here
        if _is_worktree(cwd):
            return str(cwd)
        LOG.warning(
            "Inside a .worklog/worktrees path but .git is not a file; "
            "skipping %s",
            cwd,
        )
        return None

    # Scan .worklog/worktrees/ for directories matching wl-<work_item_id>-*
    repo_root = Path.cwd().resolve()
    worktrees_dir = repo_root / DEFAULT_WORKTREE_DIR
    if worktrees_dir.exists():
        pattern = f"wl-{work_item_id}-*"
        for match_dir in worktrees_dir.glob(pattern):
            if match_dir.is_dir():
                return str(match_dir.resolve())

    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Argument list (default: sys.argv[1:]).

    Returns:
        Parsed namespace.
    """
    parser = argparse.ArgumentParser(
        description="Implement skill: deterministic implementation workflow orchestration",
    )
    parser.add_argument(
        "action",
        choices=["start", "finish", "abort", "parent", "drive", "cleanup"],
        help=(
            "Workflow phase to execute ('cleanup' removes a finished item's "
            "worktree; run it as the session's last bash command)"
        ),
    )
    parser.add_argument(
        "work_item_id",
        help="Work item ID (e.g., SA-XXXXXXXXXXX)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results in JSON format",
    )
    parser.add_argument(
        "--no-refactor",
        action="store_true",
        help="Skip the refactor step",
    )
    parser.add_argument(
        "--max-retry",
        type=int,
        default=DEFAULT_MAX_RETRY,
        help=f"Max test-fix loop retries (default: {DEFAULT_MAX_RETRY})",
    )
    parser.add_argument(
        "--commit-msg",
        default=None,
        help="Commit message override",
    )
    parser.add_argument(
        "--parent-branch",
        default=DEFAULT_PARENT_BRANCH,
        help=f"Parent branch for worktree (default: {DEFAULT_PARENT_BRANCH})",
    )
    parser.add_argument(
        "--keep-worktree",
        action="store_true",
        help=(
            "finish: defer worktree removal — the caller owns the cleanup, so "
            "the finishing session keeps its cwd. Implied for driven child "
            f"sessions ({DRIVE_RECURSION_ENV}=1); also settable with "
            f"{KEEP_WORKTREE_ENV}=1."
        ),
    )
    parser.add_argument(
        "--worktree-path",
        default=None,
        help="Override worktree path",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--allow-orphaned-stashes",
        action="store_true",
        help="Acknowledge the orphaned-stash warning and proceed (fail-open gate)",
    )
    parser.add_argument(
        "--max-child-sessions",
        type=int,
        default=DEFAULT_MAX_CHILD_SESSIONS,
        help=(
            "Max fresh sessions spawned per child when driving an epic "
            f"(default: {DEFAULT_MAX_CHILD_SESSIONS})"
        ),
    )
    parser.add_argument(
        "--child-timeout",
        type=int,
        default=None,
        help=(
            "Per-child session timeout in seconds when driving an epic "
            f"(default: {DEFAULT_CHILD_SESSION_TIMEOUT})"
        ),
    )
    parser.add_argument(
        "--finish-attempt-limit",
        type=int,
        default=None,
        help=(
            "Max failing finish executions per child in one drive invocation "
            "(default: the --max-child-sessions value)"
        ),
    )
    parser.add_argument(
        "--infra-retry-limit",
        type=int,
        default=DEFAULT_INFRA_RETRY_LIMIT,
        help=(
            "Max infrastructure-refusal retries per child in one drive "
            "invocation. A transient infrastructure refusal (e.g. "
            "local_dev_diverged, a held main-checkout lock, a dropped push) "
            "is retried on this separate bounded budget with exponential "
            "backoff — it never consumes --finish-attempt-limit and never "
            "spawns a session. "
            f"(default: {DEFAULT_INFRA_RETRY_LIMIT})"
        ),
    )
    parser.add_argument(
        "--pane-sessions",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Drive each child as an interactive Pi TUI in a sibling Herdr "
            "pane of the calling tab. Requires Herdr to be detected "
            "(HERDR_ENV=1 and a resolvable herdr binary); otherwise the "
            "headless `pi -p` spawner is used unchanged. ``--no-pane-sessions`` "
            "forces the headless path. Default: env-driven via "
            f"{PANE_SESSIONS_ENV}."
        ),
    )
    parser.add_argument(
        "--focus",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_PANE_FOCUS,
        help=(
            "With pane sessions, focus each new child pane. Default "
            "--no-focus so the caller's focus is unchanged."
        ),
    )
    parser.add_argument(
        "--finalise-stranded",
        action="store_true",
        default=False,
        help=(
            "When driving an epic, finalise stranded children (work pushed "
            "but item still in-progress) to completed/in_review once the "
            "pushed commit is verified and the worktree is clean. Read-only "
            "diagnosis remains the default."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the implement orchestration.

    Args:
        argv: Command-line arguments.

    Returns:
        Exit code: 0 = success, 1 = error, 2 = aborted.
    """
    try:
        return _main(argv)
    except KeyboardInterrupt:
        LOG.warning("Interrupted by user")
        if _work_item_id_global:
            _safety_reset_if_in_progress(_work_item_id_global)
        return 2
    except Exception as exc:  # noqa: BLE001
        LOG.error("Unhandled exception: %s", exc)
        LOG.debug(traceback.format_exc())
        if _work_item_id_global:
            _safety_reset_if_in_progress(_work_item_id_global)
        return 1


def _main(argv: list[str] | None = None) -> int:
    """Internal main with proper exception handling."""
    args = parse_args(argv)

    # Track the work item for ALL phases (start/finish/abort) so that the
    # top-level exception and signal handlers can always release the item
    # back to a valid terminal state — never leave it stuck in-progress.
    # phase_start() later overwrites these globals with the real worktree
    # path and repo root once they are known.
    _store_signal_globals("", args.work_item_id, "")
    # The ``--keep-worktree`` decision must survive into the signal handler so
    # an interrupted opt-in finish keeps the session's cwd
    # (SA-0MVTE8XAJ002IUDR AC4).
    _set_keep_worktree_global(getattr(args, "keep_worktree", False))
    _register_signal_handlers()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if args.action == "start":
        with Timer("implement") as _root_timer, Timer("phase_start"):
            result = phase_start(
                work_item_id=args.work_item_id,
                json_output=args.json,
                no_refactor=args.no_refactor,
                parent_branch=args.parent_branch,
                worktree_path_override=args.worktree_path,
                max_retry=args.max_retry,
                verbose=args.verbose,
                force=args.allow_orphaned_stashes,
            )
            _emit_timing(_root_timer)
    elif args.action == "finish":
        with Timer("implement") as _root_timer, Timer("phase_finish"):
            result = phase_finish(
                work_item_id=args.work_item_id,
                json_output=args.json,
                no_refactor=args.no_refactor,
                commit_msg_override=args.commit_msg,
                max_retry=args.max_retry,
                verbose=args.verbose,
                keep_worktree=args.keep_worktree,
            )
            _emit_timing(_root_timer)
    elif args.action == "abort":
        with Timer("implement") as _root_timer, Timer("phase_abort"):
            result = phase_abort(
                work_item_id=args.work_item_id,
                json_output=args.json,
                verbose=args.verbose,
            )
            _emit_timing(_root_timer)
    elif args.action == "cleanup":
        with Timer("implement") as _root_timer, Timer("phase_cleanup"):
            result = phase_cleanup(
                work_item_id=args.work_item_id,
                json_output=args.json,
                verbose=args.verbose,
                worktree_path=args.worktree_path,
            )
            _emit_timing(_root_timer)
    elif args.action == "parent":
        with Timer("implement") as _root_timer, Timer("phase_parent"):
            result = phase_parent(
                work_item_id=args.work_item_id,
                json_output=args.json,
                no_refactor=args.no_refactor,
                parent_branch=args.parent_branch,
                verbose=args.verbose,
            )
            _emit_timing(_root_timer)
    elif args.action == "drive":
        with Timer("implement") as _root_timer, Timer("phase_drive"):
            result = phase_drive(
                work_item_id=args.work_item_id,
                json_output=args.json,
                no_refactor=args.no_refactor,
                parent_branch=args.parent_branch,
                verbose=args.verbose,
                max_child_sessions=args.max_child_sessions,
                child_timeout=args.child_timeout,
                finish_attempt_limit=args.finish_attempt_limit,
                infra_retry_limit=args.infra_retry_limit,
                finalise_stranded=args.finalise_stranded,
                pane_sessions=args.pane_sessions,
                focus=args.focus,
            )
            _emit_timing(_root_timer)
    else:
        LOG.error("Unknown action: %s", args.action)
        return 1

    if not result.get("success"):
        return 2 if result.get("dirty_worktree") else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
