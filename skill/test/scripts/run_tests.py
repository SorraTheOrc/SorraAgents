#!/usr/bin/env python3

# <!-- REFACTOR-SA-0MVPTU74Y005WAY0
# smell: formatting
# severity: low
# description: Shebang is present but file is not executable
# -->
"""Run the full project test suite in quiet mode and report every failure.

Suites are resolved per-repo through ``full_suite_commands`` (the single
source of truth, F2 AC4):

  1. ``.pi/test-config.json`` ``suiteCommands`` when present (primary list;
     convention detection skipped)
  2. pytest: ``pytest -q -r a --disable-warnings`` — only when the repo
     declares a pytest suite (never a phantom pytest command, F2 AC3)
  3. node: ``node --test "tests/<dir>/**/*.mjs"`` per existing
     ``tests/{unit,node,cli}`` dir (npm --silent test when a test script
     exists)
  4. npm-test convention: ``npm --silent test`` for TCE-like repos (npm test
     script, no pytest config, no node suite dirs — F2 AC2)

The optional ``timeoutPerCommand`` in ``.pi/test-config.json`` overrides the
per-command timeout (F2 AC1).

Results are cached per-repo (see skill/test_cache.py) by default so repeated
verification at the same git state is served without re-executing the suite.

Concurrent suite executions are bounded host-wide via two shared semaphore
namespaces (skill/shared/process_semaphore.py): the "test" lane carries FULL
suites (TEST_MAX_CONCURRENCY, default 6 — calibrated, see the constant block)
and the "test-fast" lane carries PARTIAL runs (TEST_MAX_CONCURRENCY_FAST,
default 4), so a 1-3 s changed-scope run never queues behind a 7-minute full
suite (SA-0MVWZQ269009SJXV). TEST_LOCK_TIMEOUT (default 600s) bounds the wait
for either lane's free slot. Only ACTUAL executions acquire a slot — cache
hits are served without executing and never block on the semaphore
(SA-0MTG5U75A001F1RG). Every result reports ``slot_wait_seconds`` (queue time)
separately from ``elapsed_seconds`` (execution time) so a queue-delayed run
can never read as a slow suite (SA-0MVU8XRBQ000DG9D).

Per-suite worker pools are bounded independently of that suite ceiling
(SA-0MVE98T1M006A17T): a single vitest suite otherwise sizes its tinypool to
the CPU count (15+ forks were observed from one suite, SA-0MSAEKOQE009TEB4),
so the host-wide ceiling alone cannot bound total load. Every suite command
the runner executes inherits ``VITEST_MAX_WORKERS`` (default 4) in its child
environment, which vitest's config resolution honours and ``npm test``
propagates. The cap is env-only — command strings are never rewritten, so the
per-repo cache key is unchanged.

Nested-run isolation (SA-0MUY6U05F008RG86): a test that runs inside the full
suite already sits under an outer run that holds a host-wide slot. If it
invokes ``run_tests.py`` again (in-process or as a subprocess), the nested
run must not contend for that same pool — the outer slot is held until the
nested run finishes, so a nested acquire can wait out TEST_LOCK_TIMEOUT and
deadlock when two full suites run concurrently. Fixtures that spawn a nested
runner set ``PI_SEMAPHORE_DIR`` to a per-test isolated directory (see
``nested_semaphore_dir``) so the nested run bounds itself in its own
namespace. Top-level runs never do this, so the host-wide ceiling is
preserved for genuine parallel suites.

Emits structured per-failure records (test_name, stdout_excerpt, stack_trace)
compatible with the triage skill's check_or_create.py input.

Live-repo guard ownership: :func:`guarded_run_all` is the **single source of
truth** for the outer snapshot guard (snapshot → run → mutation check) and
the ``LIVE_REPO_GUARD_ACTIVE`` recursion marker. The CLI (:func:`main`) and
``implement.py``'s finish-gate test step both delegate to it, so no entry
point is left unguarded and no second guard implementation can diverge. When
the marker is already set by an outer guard, :func:`guarded_run_all` stands
down (the outermost guard stays the single owner). The mutation surface is
refs + local config + working tree, with an **ownership-based exclusion**: a
ref checked out in a registered worktree other than the guard's own root (plus
its ``branch.<name>.*`` config, retained worktree-track config, and
remote-tracking refs) is concurrent-infrastructure churn; the guard root's
own branch stays monitored (SA-0MV8311B3005DNG8). The guard also covers the
**main checkout** whenever the guarded root is a linked worktree — the two
exclusions are applied per checkout, so a worktree-scoped run that unlinks a
tracked file in the main checkout can no longer be reported green
(WL-0MVPE2BY9001CR9O). See also the repo-root
``conftest.py`` / ``skill/shared/live_repo_guard.py`` inner guard, which this
outer guard stands down (SA-0MUG30J5F001L57H, SA-0MUH1MJL6003767Q).

Usage:
  run_tests.py [--suite pytest|node|all] [--json] [--parent-work-item-id ID] [--rerun-failures]
  run_tests.py --summary [--summary-grep PATTERN]   (read cached summary lines, no execution)
  run_tests.py --force | --no-cache                (bypass the cache)
  run_tests.py --scope full --fingerprint SHA      (bind the run to an explicit
                                                     would-be-commit tree SHA)

Explicit tree fingerprint (--fingerprint):
  A ``--scope full`` run may be bound to an explicit would-be-commit tree SHA
  (the value ``git write-tree`` reports for a staged change set): the run is
  stored and looked up with ``fingerprint_kind="tree"``, keyed on that SHA
  and never on the loose worktree state. The binding is **fail-closed**: a
  value that is not a usable tree SHA, or a combination with a mode that
  cannot bind it (``--scope changed``, ``--summary``, ``--no-cache``), is
  rejected with exit 2 and an actionable message — never a silent fallback
  to a loose worktree key. A tree entry never satisfies a worktree request
  (and vice versa), and a tree request with no stored entry executes fresh
  and records the entry under ``fingerprint_kind="tree"`` (SA-0MW7C6AYO0062GJB).
  Callers that pass nothing keep the historical loose-worktree keys byte for
  byte.

Exit codes:
  0 - all suites passed
  1 - at least one test failed (or --summary cache miss)
  2 - runner error (missing suite binary etc.)
"""

from __future__ import annotations

import argparse
import configparser
import contextvars
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

import tomllib

_SKILLS_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_ROOT_STR = str(_SKILLS_ROOT)
if _SKILLS_ROOT_STR in sys.path:
    sys.path.remove(_SKILLS_ROOT_STR)
sys.path.insert(0, _SKILLS_ROOT_STR)

from shared.git_sandbox import (
    describe_guarded_diff,
    diff_guarded_roots,
    excluded_churn_notes,
    git_env_for_repo,
    snapshot_guarded_roots,
)
from shared.process_semaphore import Semaphore, nested_semaphore_dir  # noqa: F401
from shared.skill_extensions import (
    DATA_FILENAME,
    SkillExtensionError,
    load_extension,
)
from shared.test_tmpdir import prune_stale_test_roots, resolve_test_tmpdir
from shared.timing import Timer
from test_cache import (
    DEFAULT_TTL_SECONDS,
    STATE_CHANGED_EXIT_CODE,
    STATE_CHANGED_NOTICE_CODE,
    Runner,
    _default_runner,
    is_worktree_clean,
    query_cached,
    run_cached,
    summary_lines,
)
from test_runner import (
    canonicalize_quiet_test_command,
    executable_test_command,
    is_pytest_command,
    is_vitest_command,
    log_repository_override_scrub,
    repository_override_vars_present,
    resolve_pytest_executable,
    subprocess_env_with_scrubbed_overrides,
)

REPO_ROOT = _SKILLS_ROOT.parent

#: Runner diagnostics logger (SA-0MW3ENF32006BGN1): TMPDIR choice / prune
#: outcomes are emitted here so operators can see the disk-backed TMPDIR and
#: stale-root pruning from one place.
LOG = logging.getLogger("skill.test.run_tests")

# Cache TTL exposed as a module constant so CLI tests can backdate entries.
CACHE_TTL_SECONDS = DEFAULT_TTL_SECONDS

# Canonical pytest command (stable form for cache keying and cross-consumer
# reuse). The executable is resolved at run time via executable_test_command
# to the first probed candidate that can run pytest — the runner's own
# interpreter (``sys.executable -m pytest``) when it can, so a PATH ``pytest``
# bound to a foreign interpreter cannot shadow it (SA-0MVK154IE007OS3D); the
# resolved interpreter is recorded in the run output and cache metadata.
PYTEST_CMD = canonicalize_quiet_test_command("pytest")
NODE_SUITE_DIRS = ("tests/node", "tests/cli", "tests/unit")

# Test-run concurrency bounding (SA-0MTG5U75A001F1RG): concurrent test
# suite executions are bounded host-wide via the shared flock "test"
# semaphore (skill/shared/process_semaphore.py) — a separate namespace
# from the "audit" semaphore so the two workloads have independent
# ceilings. Cache hits never execute and never consume a slot.
#
# Full-suite ceiling calibration (SA-0MVWZQ269009SJXV; evidence committed to
# docs/dev/test-ceiling-validation.json): 6, not the original uncalibrated 2.
# Measured on the 32-thread host 2026-10-31 — 41 full suites at 382-465 s
# (median 441 s) whether one or two ran concurrently, each suite using < 1
# core, the host 92% CPU idle while this pool sat at 100% capacity (22 suites
# in 83 min), and 7-18 min slot waits for partial runs whose pytest body took
# 1-3 s. Queueing, not compute, was the binding constraint; six suites is at
# most six cores of thirty-two.
TEST_SEMAPHORE_NAME = "test"
TEST_MAX_CONCURRENCY_ENV = "TEST_MAX_CONCURRENCY"
TEST_MAX_CONCURRENCY_DEFAULT = 6
#: Independent lane for PARTIAL runs (``--scope changed``, or a ``--type``
#: other than ``full``): its own namespace, so partial runs neither queue
#: behind full suites nor evict them (SA-0MVWZQ269009SJXV AC1). The ceiling is
#: separate from the full-suite ceiling: partial runs are short, so the pool is
#: sized for burst absorbtion rather than for suite throughput.
TEST_FAST_SEMAPHORE_NAME = "test-fast"
TEST_MAX_CONCURRENCY_FAST_ENV = "TEST_MAX_CONCURRENCY_FAST"
TEST_MAX_CONCURRENCY_FAST_DEFAULT = 4
#: Lane names carried on every suite result and used to resolve the
#: (semaphore namespace, ceiling) pair.
TEST_SLOT_LANE_FULL = "full"
TEST_SLOT_LANE_FAST = "fast"
TEST_LOCK_TIMEOUT_ENV = "TEST_LOCK_TIMEOUT"
TEST_LOCK_TIMEOUT_DEFAULT = 600.0
#: Per-waiter stagger fraction of the bounded wait (SA-0MVEDOF7A004TSWD):
#: concurrent waiters expire at slightly different instants within the
#: TEST_LOCK_TIMEOUT bound instead of all timing out together and re-entering
#: the pool in lockstep (the thundering-herd wave). 0.0 disables the stagger.
TEST_LOCK_JITTER_ENV = "TEST_LOCK_JITTER"
TEST_LOCK_JITTER_DEFAULT = 0.1

#: Stable, machine-readable notice codes carried on a suite result that fails
#: WITHOUT structured test failures (WL-0MVGUT787000LNDG): no free test slot,
#: command-not-found, suite timeout, killed child, unparsable non-zero exit.
#: Consumers (e.g. ``implement.py``'s finish gate) classify on the code —
#: never on the human-readable notice text — so rewording a notice can never
#: silently reclassify a genuine test failure as an infrastructure condition.
NOTICE_SLOT_BUSY = "slot_busy"
NOTICE_COMMAND_NOT_FOUND = "command_not_found"
NOTICE_SUITE_TIMEOUT = "suite_timeout"
#: A command's guarded checkout moved while it executed (SA-0MW3FF18I003KGZE
#: AC1): the mixed-tree verdict is quarantined and reported non-green with the
#: distinct ``STATE_CHANGED_EXIT_CODE``, never parsed as a code failure. The
#: code is the cache's stable notice (:data:`test_cache.STATE_CHANGED_NOTICE_CODE`)
#: so the runner and the cache cannot drift.
NOTICE_STATE_CHANGED_DURING_RUN = STATE_CHANGED_NOTICE_CODE

#: Stable code for the **non-fatal** resolution notice that a pinned
#: ``.pi/test-config.json`` ``suiteCommands`` list does not cover some of the
#: project's hidden skill test directories (SA-0MVQQ9NV4005UKYZ AC3). It is a
#: notice, never a failure: the pinned list is still returned verbatim and the
#: exit status is unchanged.
NOTICE_HIDDEN_SKILL_TESTS_UNCOVERED = "hidden_skill_tests_uncovered"
#: The child was terminated by a signal (``returncode < 0``): an OOM kill or a
#: worker/supervisor crash. No failure is parsable from a killed child, so
#: without this code the run reads as an unexplained non-zero exit
#: (SA-0MVU8XRBQ000DG9D).
NOTICE_KILLED = "killed"
#: The child exited non-zero and NO structured test failure could be parsed
#: from its output (bad flag, unloadable config, missing binary wrapper, …).
#: Structured failures always win: this code applies only when ``failures``
#: is empty (SA-0MVU8XRBQ000DG9D).
NOTICE_UNPARSED_FAILURE = "unparsed_failure"

#: Stable code for the **non-fatal** notice that a retry-runner stage was
#: accepted (``status=0`` + ``retry-clean``/``accepted-transient``) and its
#: superseded-attempt failures were discarded as diagnostics
#: (CG-0MWEHG69O005VM17). A notice, never a failure: ``success`` stays green
#: and the exit status is unchanged — the release gate must not need a re-run
#: after an accepted retry. The discarded records also live on the structured
#: ``discarded_failures`` field of the suite result.
NOTICE_RETRY_DISCARDED = "retry_discarded_failures"

#: Prefix of the no-free-slot notice produced by :func:`_test_concurrency_slot`.
#: Exposed as a stable constant so a consumer handling a legacy result that
#: predates ``notice_code`` can classify slot contention without duplicating
#: the wording (WL-0MVGUT787000LNDG).
SLOT_BUSY_NOTICE_PREFIX = "test concurrency slot busy:"

#: Bound for the stdout/stderr tail carried on a suite result
#: (WL-0MVGUT787000LNDG): large enough to include a stack trace, small enough
#: that JSON reports stay readable. ``implement.py`` re-tails the aggregate to
#: its own smaller bound for the operator message.
NOTICE_OUTPUT_TAIL_CHARS = 4000


def _output_tail(text: str | None, limit: int = NOTICE_OUTPUT_TAIL_CHARS) -> str:
    """Return a bounded tail of *text* — the last ``limit`` characters.

    Empty/None input yields an empty string; short text is returned whole.
    """
    if not text:
        return ""
    text = text.rstrip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _measured_seconds(samples: list[float]) -> float | None:
    """Total measured command-execution time, or ``None`` when none ran.

    ``None`` (never ``0``) distinguishes an *unmeasured* execution — every
    command was served from the cache, or a slot wait expired before the
    command was spawned — from one that finished in under a millisecond, so
    a consumer can never read "unavailable" as "instantaneous"
    (SA-0MVU8XRBQ000DG9D).
    """
    return sum(samples) if samples else None


#: Per-suite vitest/tinypool worker-pool bound (SA-0MVE98T1M006A17T): the
#: complement to the host-wide suite ceiling. A single vitest suite normally
#: sizes its pool to the CPU count (15+ tinypool forks observed from one
#: suite, SA-0MSAEKOQE009TEB4), so bounding concurrent *suites* alone leaves
#: per-suite fan-out unbounded. Every suite command the runner executes gets
#: this value in its child environment; vitest's config resolution reads it
#: (``resolved.maxWorkers``) and ``npm test`` propagates it to the vitest
#: process. The default matches the measured per-suite cap already applied to
#: the consumer repos (ContextHub / Tableau-Card-Engine, SA-0MSAK2ZH6009Z3TW)
#: and keeps one vitest run to ~4 node workers on the 16-core host.
TEST_VITEST_MAX_WORKERS_ENV = "VITEST_MAX_WORKERS"
TEST_VITEST_MAX_WORKERS_DEFAULT = 4

#: Recursion marker shared with the pytest conftest guard (F5). Set by
#: whichever guard owns the checkout first; nested pytest/run_tests stand down.
LIVE_REPO_GUARD_ACTIVE_ENV = "LIVE_REPO_GUARD_ACTIVE"

#: Explicit operator opt-out from the strict release-gate refusal. Scrub is
#: always applied; this only lets a rare legitimate override-vars run proceed
#: (with a loud warning) when ``--strict-git-env`` was requested.
STRICT_GIT_ENV_OPT_OUT_ENV = "RUN_TESTS_ALLOW_REPO_OVERRIDES"

# pytest config markers, mirroring implement.py's _has_pytest_markers so the
# test/implement/audit skills agree on whether a repo has a pytest suite
# (SA-0MSQ72BVV0011SRU AC3; single source of truth per F2 AC4).
_PYTEST_CONFIG_MARKERS = (
    ("pyproject.toml", "[tool.pytest.ini_options]"),
    ("setup.cfg", "[tool:pytest]"),
    ("tox.ini", "[pytest]"),
)

# The per-project suite-command extension file (F2 AC1):
# <project_root>/.pi/test-config.json with an optional ``suiteCommands`` list
# (the primary command list; convention detection is skipped when present) and
# an optional ``timeoutPerCommand`` (per-command timeout in seconds).
TEST_CONFIG_FILE = ".pi/test-config.json"

# --- Test-type (profile) selection (SA-0MTJQB2MA008HMO6) ------------------
# Minimum types every project accepts. ``full`` is the default and preserves
# the existing full-suite contract; ``unit``/``smoke`` fall back to convention
# detection (``tests/unit``, ``tests/smoke``) when no local extension defines
# them. A project may define additional types via its local extension
# (``.pi/skills_extensions/test/extension.json`` → ``types`` map).
MINIMUM_TEST_TYPES = ("smoke", "unit", "full")
#: Type used when ``--type`` is omitted (backwards compatibility, AC5).
DEFAULT_TEST_TYPE = "full"
#: The global skill name whose project extension supplies the type map.
TEST_SKILL_NAME = "test"


def detect_project_root() -> Path:
    """Resolve the project the CLI should target, from the invoking cwd.

    Uses ``git rev-parse --show-toplevel`` at CLI time (mirroring the audit
    skill's ``TARGET_PROJECT_ROOT``, SA-0MSNQV9J20010LE7) so that running
    ``run_tests.py`` from a non-framework project (e.g. the llm repo) tests
    and caches THAT project's suite — not the framework's install location.
    Falls back to ``REPO_ROOT`` when git is unavailable or cwd is not inside
    a git repo (preserving legacy behavior).

    Returns:
        The project root Path.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            root = Path(proc.stdout.strip())
            if root.is_dir():
                return root
    except (OSError, subprocess.SubprocessError):
        pass
    return REPO_ROOT

# "Red" short-test-summary lines emitted by pytest ``-r a`` on a non-zero
# run: ``FAILED <nodeid> - <message>`` for test failures and
# ``ERROR <nodeid> - <message>`` for collection/setup/teardown errors (the
# message is absent for some collection errors). Parsing only ``FAILED``
# left error-only runs — which exit non-zero with no FAILED line — recorded
# as a bare ``<suite exited N>: pytest ...`` entry with no named test, which
# triage cannot act on (SA-0MSRN0Q64005YR5A).
_RED_SUMMARY_RE = re.compile(
    r"^(?P<kind>FAILED|ERROR)\s+(?P<name>.+?)(?:\s+-\s+(?P<message>.*))?$",
    re.MULTILINE,
)
_SECTION_RE = re.compile(r"^_{5,}\s+(.+?)\s+_{5,}$", re.MULTILINE)
_NODE_NOT_OK_RE = re.compile(r"^not ok\s+\d+\s*-\s*(.+)$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Scope-aware test selection
# ---------------------------------------------------------------------------

TRACKED_SOURCE_EXTENSIONS = (".py", ".mjs", ".js", ".ts", ".tsx")

#: Suffixes recognised as test files by the convention mapper. The
#: TypeScript/JavaScript forms cover vitest (``*.test.ts`` / ``*.spec.ts``),
#: which previously mapped to nothing and forced the full suite
#: (SA-0MVL7YOMJ006UMDO). ``_test.mjs`` and ``*.test.mjs`` keep the node
#: suite conventions.
JS_TS_TEST_SUFFIXES = (
    ".test.mjs",
    ".spec.mjs",
    "_test.mjs",
    ".test.js",
    ".spec.js",
    ".test.ts",
    ".spec.ts",
    ".test.tsx",
    ".spec.tsx",
)

#: Directories never scanned for test files (VCS metadata, dependency trees,
#: build output, caches). Pruning them keeps the scan bounded in a monorepo
#: and avoids selecting a vendored ``node_modules`` test.
_TEST_SCAN_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".worklog",
        "node_modules",
        "dist",
        "build",
        "coverage",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
    }
)


def _git_inspection_env(root: Path) -> dict[str, str]:
    """Environment for a git command that *inspects* ``root``.

    Delegates to :func:`shared.git_sandbox.git_env_for_repo`: repository
    overrides are scrubbed (F2, SA-0MUIA3OE40001QJX) and an ambient
    ``GIT_INDEX_FILE`` is honoured only when it belongs to *root*'s own
    repository. The finish gate exports a temporary would-be-commit index for
    the suite it runs (SA-0MVQHVGFR006W8LG), so the repository under test is
    measured against that staged tree, while a fixture repository never reads
    — or refreshes, which writes — another repository's index. Inheriting it
    made a fixture report every tracked file as changed and invalidated the
    gate's evidence for the tested tree (SA-0MVTET8KQ0031R6Z).

    Args:
        root: The repository the git command operates on.

    Returns:
        A child environment mapping for the git subprocess.
    """
    return git_env_for_repo(root)


def compute_changed_files(
    project_root: Path | None = None,
    base_ref: str = "origin/dev",
) -> set[str]:
    """Return the set of files changed between *base_ref* and HEAD.

    Base resolution order (first match wins):

    1. ``<base_ref>`` (default ``origin/dev``) — the remote-tracking dev ref
    2. ``dev`` local branch — when the remote ref is absent
    3. ``HEAD~1`` — when neither dev ref exists (fallback for repos without
       a dev branch, so the diff is against the last commit)

    New (untracked, not-yet-committed) files ARE included: during the
    implement flow the tests run before the first commit, so a newly created
    test file has no diff entry yet and would otherwise be invisible to
    changed-scope selection. ``git ls-files --others --exclude-standard``
    adds them while keeping ignored build/dependency churn out. The
    merge-base is used so only files changed on the current branch since it
    forked from the base are returned — files changed on the base after the
    fork point are not attributed to this branch.

    Args:
        project_root: Repo root (default: detected from calling repo).
        base_ref: Preferred base ref for the diff (default ``origin/dev``).

    Returns:
        Set of repo-relative changed file paths (POSIX separators).
    """
    root = Path(project_root or REPO_ROOT).resolve()

    base = _resolve_merge_base(root, base_ref)
    if base is None:
        # No base ref at all — nothing to diff against.
        return set()

    proc = subprocess.run(
        ["git", "diff", "--name-only", base],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=_git_inspection_env(root),
    )
    if proc.returncode != 0:
        return set()
    changed = {line.strip() for line in proc.stdout.splitlines() if line.strip()}

    # New (untracked) files are part of the working-tree change set but have
    # no diff entry against *base*. Include them so a newly created test file
    # (the common implement-flow case: tests run before the first commit) is
    # selectable. ``--exclude-standard`` keeps ignored files out. The
    # inspection environment is the same one the diff uses, so a *foreign*
    # gate index is never inherited — under one, every fixture file looks
    # untracked and would both pollute the change set and invalidate the
    # gate's evidence (SA-0MVTET8KQ0031R6Z).
    try:
        others = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_git_inspection_env(root),
        )
    except (OSError, subprocess.TimeoutExpired):
        others = None
    if others is not None and others.returncode == 0:
        changed.update(
            line.strip() for line in others.stdout.splitlines() if line.strip()
        )
    return changed

def _resolve_merge_base(root: Path, base_ref: str) -> str | None:
    """Find the merge-base commit for diffing, following the fallback chain.

    Returns None only when no usable base exists (no commits yet, or git
    unavailable). Logs a warning for each fallback so operators understand
    which base produced the diff.
    """
    candidates = [base_ref]
    if base_ref != "dev":
        candidates.append("dev")
    candidates.append("HEAD~1")

    for candidate in candidates:
        try:
            proc = subprocess.run(
                ["git", "merge-base", candidate, "HEAD"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=_git_inspection_env(root),
            )
        except (OSError, subprocess.TimeoutExpired):
            # Missing cwd / git unavailable → no diff base → caller falls
            # back to full scope rather than crashing the run.
            return None
        if proc.returncode == 0 and proc.stdout.strip():
            if candidate != base_ref:
                print(
                    f"run_tests: base ref '{base_ref}' not found — using '{candidate}' "
                    "for changed-file detection",
                    file=sys.stderr,
                )
            return candidate
    return None


def map_changed_to_tests(
    project_root: Path | None = None,
    changed_files: set[str] | None = None,
) -> set[str]:
    """Map changed source files to affected test files.

    Two mechanisms, unioned:

    1. **Convention mapping**: ``src/foo.py`` → ``tests/test_foo.py``;
       ``tests/test_foo.py`` itself; any test file in the repository whose
       basename matches the changed file's stem. Recognised test files are
       pytest (``test_*.py`` / ``*_test.py``), node (``*.test.mjs`` /
       ``*_test.mjs`` / ``*.spec.mjs``) and vitest/TypeScript
       (``*.test.ts`` / ``*.spec.ts``, plus the ``.js``/``.tsx`` variants).
    2. **Import-graph expansion**: AST-scan every test file once; for each
       changed module (dotted name derived from its path relative to the
       project root), find test files whose import statements reference the
       changed module OR any module that (transitively) imports it. This
       catches indirect breakage — e.g. ``utils.py`` changed →
       ``test_report.py`` imported via a chain. (Python only; the TypeScript
       side uses the conservative stem convention.)

    ``changed_files`` defaults to :func:`compute_changed_files` against the
    calling repo. Non-source changes (README, yaml, …) never map to tests
    unless they are themselves test files.

    Args:
        project_root: Repo root.
        changed_files: Changed files (default: computed).

    Returns:
        Set of test-file paths (repo-relative, POSIX).
    """
    root = Path(project_root or REPO_ROOT).resolve()
    changed = changed_files if changed_files is not None else compute_changed_files(root)

    if not changed:
        return set()

    # Every recognisable test file in the repo — py, node and vitest/TS — so
    # a changed source can select its tests by convention regardless of where
    # the suite lives (``tests/``, ``test/`` or co-located ``src/``).
    all_tests: set[str] = set(_iter_test_files(root))

    # 1. Convention mapping
    selected: set[str] = set()
    for changed_file in changed:
        rel = Path(changed_file)
        if _is_test_file(rel):
            # Only select the test when it still exists on disk: a test file
            # that was deleted/renamed away still appears in `git diff` but
            # must not reach the pytest command line (exit 4 → phantom
            # failure). See LP-0MTZYRTNF0092JKW.
            if (root / changed_file).exists():
                selected.add(changed_file)
            continue
        if rel.suffix not in TRACKED_SOURCE_EXTENSIONS:
            continue  # non-source change → no test selection

        # Direct convention: changed_stem → the matching test file name in
        # any test directory (pytest, node and vitest/TypeScript forms).
        candidates = {
            f"test_{rel.stem}.py",
            f"{rel.stem}_test.py",
            f"test_{rel.stem}.mjs",
            f"{rel.stem}_test.mjs",
            f"{rel.stem}.test.mjs",
            f"{rel.stem}.spec.mjs",
            f"{rel.stem}.test.js",
            f"{rel.stem}.spec.js",
            f"{rel.stem}.test.ts",
            f"{rel.stem}.spec.ts",
            f"{rel.stem}.test.tsx",
            f"{rel.stem}.spec.tsx",
        }
        for test in all_tests:
            test_rel = Path(test)
            if test_rel.name in candidates:
                selected.add(test)
            elif (
                rel.name == "__init__.py"
                and test_rel.name.startswith("test_")
                and str(rel.parent) == str(test_rel.parent)
            ):
                # Package init changed → every test under that package tree.
                selected.add(test)

    # 2. Import-graph expansion
    if selected or _has_python_changes(changed):
        selected |= _expand_by_imports(root, all_tests, changed)

    # Final existence filter: convention mapping scans the filesystem so its
    # entries exist, but a changed test file added directly above (or an
    # import-graph entry pointing at a removed path) may not. Guard the
    # contract that every returned path exists on disk (LP-0MTZYRTNF0092JKW).
    return {f for f in selected if (root / f).exists()}


def _is_test_file(rel: Path) -> bool:
    """True for names matching a recognised test-file convention.

    Covers pytest (``test_*.py`` / ``*_test.py``), node
    (``*_test.mjs`` / ``*.test.mjs`` / ``*.spec.mjs``) and vitest/TypeScript
    (``*.test.ts`` / ``*.spec.ts`` and the ``.js``/``.tsx`` variants).
    """
    name = rel.name
    return name.startswith("test_") or name.endswith(
        ("_test.py", *JS_TS_TEST_SUFFIXES)
    )


def _iter_test_files(root: Path) -> Iterator[str]:
    """Yield repo-relative paths of every recognisable test file under *root*.

    The whole tree is scanned (pruning VCS, dependency, build and worklog
    directories) so co-located vitest tests (``src/foo.test.ts``) and the
    conventional ``tests/``/``test/`` trees are both discovered — not only
    the Python/node suite directories the previous implementation knew about.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _TEST_SCAN_EXCLUDED_DIRS]
        for filename in filenames:
            rel = Path(dirpath, filename).relative_to(root)
            if _is_test_file(rel):
                yield rel.as_posix()


def _has_python_changes(changed: set[str]) -> bool:
    return any(p.endswith(".py") for p in changed)


def _expand_by_imports(
    root: Path,
    all_tests: set[str],
    changed: set[str],
) -> set[str]:
    """AST scan: find test files importing changed modules (transitively).

    Builds a project-wide module import graph from every ``.py`` file (source
    and test): module name → directly-imported dotted names (relative imports
    resolved against the file's package). Node names are aliased by basename
    (``src/foo.py`` → ``src.foo`` AND ``foo``) so repos that expose a source
    root on the import path (e.g. ``src/`` on ``sys.path``, so tests import
    ``foo`` while the file lives at ``src/foo.py``) still match. For each
    changed module (also matched by basename), a test file is affected when
    it directly imports the changed module, or imports any module that
    (transitively) imports it — so a change to ``utils.py`` triggers
    ``test_report.py`` that only imports ``report.py`` which imports
    ``utils.py``.

    The mapping is deliberately approximate: dynamic imports, ``__import__``,
    importlib, and namespace-package aliasing are not resolved; same-named
    modules in different packages are conflated (over-matching only — extra
    tests run, never fewer). This is a fast-feedback heuristic, NOT a
    correctness gate — the full suite is the push-time gate.
    """
    changed_targets: set[str] = set()
    for p in changed:
        if not p.endswith(".py"):
            continue
        full = _path_to_module(p)
        if full:
            changed_targets.add(full)
        changed_targets.add(Path(p).stem)
    if not changed_targets:
        return set()

    # module name → directly-imported dotted names, parsed once for every
    # project .py file (no double-parsing: tests are also in the rglob).
    # Each node is registered under BOTH its path-derived name and its
    # basename so imports in either namespace resolve.
    module_imports: dict[str, set[str]] = {}
    for py_file in root.rglob("*.py"):
        rel = str(py_file.relative_to(root))
        mod = _path_to_module(rel)
        if mod is None:
            continue
        imports = _ast_imports(root, rel)
        module_imports[mod] = imports
        module_imports[py_file.stem] = imports

    # BFS: does module *m* (transitively) import module *target*?
    def imports_module(m: str, target: str) -> bool:
        seen: set[str] = set()
        stack = [m]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if cur == target:
                return True
            stack.extend(module_imports.get(cur, ()))
        return False

    affected: set[str] = set()
    for test in all_tests:
        test_imports = module_imports.get(_path_to_module(test) or "", set())
        for mod in test_imports:
            if any(mod == cm or imports_module(mod, cm) for cm in changed_targets):
                affected.add(test)
                break
    return affected


def _path_to_module(path: str) -> str | None:
    """Derive a dotted module name from a repo-relative .py path."""
    p = Path(path)
    if p.suffix != ".py":
        return None
    if p.name == "__init__.py":
        return ".".join(p.parts[:-1]) if p.parts[:-1] else None
    parts = list(p.parts)
    if parts:
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def _ast_imports(root: Path, test_file: str) -> set[str]:
    """Return dotted module names imported by *test_file* (AST, best-effort).

    Relative imports are resolved against the importing file's package so
    ``from .utils import x`` inside ``src/foo.py`` yields ``src.utils``.
    """
    import ast

    path = root / test_file
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()

    pkg_parts = Path(test_file).parent.parts
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            name = node.module or ""
            if level:
                # level=1 → same package; level=2 → parent package, etc.
                parts = list(pkg_parts)
                if parts and level > 1:
                    parts = parts[: -(level - 1)]
                resolved = ".".join([*parts, name] if name else parts)
                imports.add(resolved)
            elif name:
                imports.add(name)
    return imports


#: Stable reason strings returned by :func:`changed_scope_selection` when no
#: subset is available. Consumers surface the specific cause instead of a
#: generic list of every possible cause (SA-0MVL7YOMJ006UMDO).
CHANGED_SCOPE_NO_CHANGES = "no changed files"
CHANGED_SCOPE_NO_SOURCE = "no changed source or test files"
CHANGED_SCOPE_NO_GROUP = "no matching group and no convention-matched test files"
CHANGED_SCOPE_NO_TESTS = "no convention-matched test files"
CHANGED_SCOPE_NO_TOOLING = "selected test files have no runnable suite tooling"
#: A group matched, but the change set also touches files its ``paths`` spec
#: does not cover and no file-level command could be resolved for them, so a
#: group-only command set would silently skip them (SA-0MWA7OD8O001945I).
CHANGED_SCOPE_GROUP_INCOMPLETE = (
    "matched group leaves changed files with no runnable test selection"
)


class ChangedScopeSelection(NamedTuple):
    """Result of changed-file → test-command selection.

    ``commands`` is ``None`` when no safe subset is available, in which case
    ``reason`` names the specific cause. ``group`` records the named test
    group that matched when selection came from ``.pi/test-config.json``.
    """

    commands: list[str] | None
    reason: str
    group: str | None = None


def _changed_outside_group_paths(
    changed: set[str], group_name: str, project_root: Path
) -> set[str]:
    """Return the changed paths the named group's ``paths`` spec does not cover.

    A matched group runs a *coarse* command set (e.g. one directory), so any
    changed file outside its ``paths`` patterns is invisible to that command
    and needs its own file-level selection
    (SA-0MWA7OD8O001945I).

    Args:
        changed: The changed-file set.
        group_name: The group selected by :func:`select_group_from_changed_files`.
        project_root: Project root holding ``.pi/test-config.json``.

    Returns:
        Changed paths not matched by any pattern in the group's ``paths``.
        Empty when the group spec is unavailable (nothing to subtract, so the
        caller keeps the historical group-only behaviour).
    """
    groups = _read_groups(project_root) or {}
    spec = groups.get(group_name)
    if spec is None:
        return set()
    paths_spec = spec.get("paths", [])
    return {p for p in changed if not _paths_match_spec(p, paths_spec)}


def _union_commands(*command_lists: list[str]) -> list[str]:
    """Union command lists in argument order, de-duplicated.

    Order is deterministic: the declared group commands first, then each
    file-level command, with later duplicates dropped. A list that already
    appears earlier is never repeated, so a group whose command happens to
    equal the file-level command runs once.
    """
    merged: list[str] = []
    seen: set[str] = set()
    for commands in command_lists:
        for command in commands:
            if command in seen:
                continue
            seen.add(command)
            merged.append(command)
    return merged


def _selectable_changed(changed: set[str]) -> set[str]:
    """Return changed paths that can drive test selection.

    A path is selectable when it is itself a recognised test file or a
    tracked source extension. Documentation/config-only change sets return
    an empty set (nothing to map to tests).
    """
    return {
        path
        for path in changed
        if _is_test_file(Path(path)) or Path(path).suffix in TRACKED_SOURCE_EXTENSIONS
    }


def repo_uses_vitest(project_root: Path | None = None) -> bool:
    """Whether *project_root* runs its TypeScript tests with vitest.

    Detected from a local ``node_modules/.bin/vitest`` binary or from
    ``package.json`` (a ``vitest`` dependency or a script whose command
    mentions vitest). This gates the convention-mapped ``*.test.ts`` /
    ``*.spec.ts`` commands: without a known runner the selector reports
    "no runnable suite tooling" instead of emitting a bogus command.
    """
    root = Path(project_root or REPO_ROOT).resolve()
    if (root / "node_modules" / ".bin" / "vitest").exists():
        return True
    pkg_json = root / "package.json"
    if not pkg_json.is_file():
        return False
    try:
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    for key in ("dependencies", "devDependencies"):
        deps = data.get(key)
        if isinstance(deps, dict) and "vitest" in deps:
            return True
    scripts = data.get("scripts")
    if isinstance(scripts, dict):
        return any(
            isinstance(value, str) and "vitest" in value
            for value in scripts.values()
        )
    return False


def _vitest_test_command(root: Path, files: list[str]) -> str | None:
    """Build a ``vitest run <files>`` command, or None when vitest is absent."""
    if not files or not repo_uses_vitest(root):
        return None
    local_bin = root / "node_modules" / ".bin" / "vitest"
    if local_bin.exists():
        executable = "node_modules/.bin/vitest"
    else:
        # A declared-but-not-yet-installed dependency: resolve the local
        # package without allowing npx to fetch a different version.
        executable = "npx --no-install vitest"
    file_args = " ".join(shlex.quote(f) for f in files)
    return f"{executable} run {file_args}"


def _commands_for_selected_tests(root: Path, selected: set[str]) -> list[str]:
    """Build runnable commands for *selected* test files, grouped by tooling.

    - pytest for ``.py`` tests (only when the repo declares a pytest suite),
    - ``node --test`` per node suite directory for ``.mjs`` tests,
    - ``vitest run`` for TypeScript/JavaScript tests when vitest is declared.

    Files are always passed explicitly so a scoped run is deterministic and
    cache-keyed distinctly from the full suite.
    """
    commands: list[str] = []

    pytest_files = sorted(
        f for f in selected if f.endswith(".py") and (root / f).is_file()
    )
    if pytest_files and repo_has_pytest_suite(root):
        file_args = " ".join(shlex.quote(f) for f in pytest_files)
        commands.append(canonicalize_quiet_test_command(f"pytest {file_args}"))

    node_files = sorted(f for f in selected if f.endswith(".mjs"))
    if node_files:
        # Group by node suite dir (tests/node, tests/cli, tests/unit) so we
        # emit one node --test command per dir, mirroring full_suite_commands.
        by_dir: dict[str, list[str]] = {}
        for f in node_files:
            top = f.split("/", 1)[0]
            by_dir.setdefault(top, []).append(f)
        for dirname, files in sorted(by_dir.items()):
            if not (root / dirname).is_dir():
                continue
            file_args = " ".join(shlex.quote(f) for f in files)
            commands.append(f"node --test {file_args}")

    js_ts_files = sorted(
        f
        for f in selected
        if f.endswith((".ts", ".tsx", ".js")) and not f.endswith(".mjs")
    )
    vitest_cmd = _vitest_test_command(root, js_ts_files)
    if vitest_cmd is not None:
        commands.append(vitest_cmd)

    return commands


def changed_scope_selection(
    project_root: Path | None = None,
    base_ref: str = "origin/dev",
    changed_files: set[str] | None = None,
) -> ChangedScopeSelection:
    """Resolve the changed-file subset and the reason when none is available.

    Selection order:

    1. **Declared ``groups``** in ``.pi/test-config.json`` — the smallest
       group whose ``paths`` match a changed file wins and its ``commands``
       run. Any changed file outside that group's ``paths`` is then unioned
       in from the file-level selection below (group commands first, then
       sorted file-level commands, de-duplicated), so a matched group can
       never silently drop an edit-authored test file
       (SA-0MWA7OD8O001945I). When nothing lies outside the group the result
       is the group's commands alone, unchanged. When the outside files are
       test/source files with no runnable file-level command, the selection
       fails closed (``commands is None``) so the caller runs the full suite.
       ``suiteCommands`` no longer disables selection: it defines the *full*
       suite only, never the changed-scope subset.
    2. **Convention mapping** — a changed test file itself, or a source whose
       stem maps to a test file (pytest, node ``.mjs``, vitest
       ``.test.ts``/``.spec.ts``).
    3. **Full-scope fallback**, with a specific ``reason``.

    *changed_files* may be passed in to avoid a second ``git diff`` when the
    caller already computed it.
    """
    root = Path(project_root or REPO_ROOT).resolve()
    changed = (
        changed_files
        if changed_files is not None
        else compute_changed_files(root, base_ref=base_ref)
    )
    if not changed:
        return ChangedScopeSelection(None, CHANGED_SCOPE_NO_CHANGES)

    # 1. Declared named groups (coarser than file-level, but still targeted).
    # A group's commands cover only its ``paths``; any changed file outside
    # them is unioned in from the file-level selection rather than dropped
    # (SA-0MWA7OD8O001945I). A group-only match keeps the historical result
    # byte for byte.
    group_name = select_group_from_changed_files(changed, root)
    if group_name is not None:
        group_cmds = group_commands(group_name, root)
        if group_cmds:
            uncovered = _changed_outside_group_paths(changed, group_name, root)
            extra_cmds: list[str] = []
            uncovered_selected: set[str] = set()
            if uncovered:
                uncovered_selected = {
                    f
                    for f in map_changed_to_tests(root, uncovered)
                    if (root / f).exists()
                }
                extra_cmds = _commands_for_selected_tests(root, uncovered_selected)
                if not extra_cmds and _selectable_changed(uncovered):
                    # The group cannot prove coverage of those files and no
                    # file-level command exists for them: fail closed to the
                    # full suite instead of silently dropping a changed test
                    # file (AC1). Non-selectable changes (docs, config) are
                    # not tests and never force the fallback.
                    return ChangedScopeSelection(
                        None, CHANGED_SCOPE_GROUP_INCOMPLETE, group_name
                    )
            commands = _union_commands(list(group_cmds), extra_cmds)
            reason = f"matched group '{group_name}'"
            if extra_cmds:
                reason += (
                    f" + {len(uncovered_selected)} changed test file(s) "
                    "outside the group"
                )
            return ChangedScopeSelection(commands, reason, group_name)

    groups_declared = _read_groups(root) is not None
    if not _selectable_changed(changed):
        return ChangedScopeSelection(None, CHANGED_SCOPE_NO_SOURCE)

    # 2. Convention mapping. The existence filter never emits a command that
    # references a path absent from the worktree (LP-0MTZYRTNF0092JKW).
    selected = {f for f in map_changed_to_tests(root, changed) if (root / f).exists()}
    commands = _commands_for_selected_tests(root, selected)
    if commands:
        return ChangedScopeSelection(
            commands, f"selected {len(selected)} affected test file(s)"
        )

    if selected:
        return ChangedScopeSelection(None, CHANGED_SCOPE_NO_TOOLING)
    if groups_declared:
        return ChangedScopeSelection(None, CHANGED_SCOPE_NO_GROUP)
    return ChangedScopeSelection(None, CHANGED_SCOPE_NO_TESTS)


def changed_scope_commands(
    project_root: Path | None = None,
    base_ref: str = "origin/dev",
    changed_files: set[str] | None = None,
) -> list[str] | None:
    """Return the test commands for changed-file scope, or None for full scope.

    Thin wrapper over :func:`changed_scope_selection` for callers that only
    need the commands. Selection now honours declared test ``groups`` and
    TypeScript/vitest ``*.test.ts``/``*.spec.ts`` files, and is NOT disabled
    by ``suiteCommands`` (which describes the full suite only).

    **Hidden project-local skill tests are enforced at full scope only**
    (SA-0MVQJSGLY0098DVB D5): ``changed_scope_commands`` never emits the
    extra hidden-skill-test command. A changed test file under
    ``.pi/skills/<name>/tests`` is selected individually like any other
    changed test file, but the directory-wide extra command is resolved only
    through :func:`full_suite_commands` (the ``full`` scope / ``full`` group
    / default type). This keeps changed-scope runs fast and regression-safe.

    Returns None (meaning "fall back to the full suite") when no changed
    files exist, only non-source changes were made, no group matches, no
    test file maps to the change, the selected tests have no runnable
    tooling, or a matched group leaves changed source/test files with no
    runnable selection of their own. Call :func:`changed_scope_selection`
    when the specific fallback reason is needed.
    """
    return changed_scope_selection(
        project_root=project_root,
        base_ref=base_ref,
        changed_files=changed_files,
    ).commands


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def pytest_command() -> str:
    """Return the quiet canonicalized pytest command."""
    return PYTEST_CMD


def repo_has_pytest_suite(project_root: Path | None = None) -> bool:
    """Whether *project_root* declares a runnable pytest suite.

    True when the repo carries a pytest config marker (pytest.ini, or the
    pytest section of pyproject.toml / setup.cfg / tox.ini) or pytest-style
    test files (``tests/``/``test/`` dirs containing ``*.py``, or root-level
    ``test_*.py`` / ``*_test.py``). Importability is deliberately NOT probed:
    the runner process always provides pytest, so the presence of markers or
    test files is the discriminator that decides whether the pytest suite
    command applies to this repo (SA-0MSQ72BVV0011SRU AC3).

    This is the single source of truth for pytest-suite detection: the audit
    skill imports it (F2 AC4) instead of duplicating the markers.
    """
    root = Path(project_root or REPO_ROOT).resolve()
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
    for dirname in ("tests", "test"):
        test_dir = root / dirname
        if test_dir.is_dir() and any(test_dir.rglob("*.py")):
            return True
    return bool(
        any(root.glob("test_*.py")) or any(root.glob("*_test.py"))
    )


#: Project-local skill tests live under ``.pi/skills/<name>/`` in one of these
#: two conventional directories (SA-0MVQJSGLY0098DVB D1).
_HIDDEN_SKILL_TEST_DIRNAMES = ("tests", "test")
#: Only this hidden root is scanned — ``.claude/``, ``.git/`` and ``.worklog/``
#: are never traversed (D1).
_HIDDEN_SKILL_TEST_ROOT = (".pi", "skills")


def hidden_skill_test_dirs(project_root: Path | None = None) -> list[str]:
    """Return project-local hidden skill test directories, sorted.

    **Why this exists.** pytest's default ``norecursedirs`` includes ``.*``,
    so a recursive ``pytest`` run from the repository root never traverses
    ``.pi/``: the tests a project ships with its *project-local* skills
    (``<root>/.pi/skills/<name>/tests``) are silently absent from the
    "full" suite (measured on the llm repo: 5 264 collected recursively vs
    529 in the skill's own test directory — SA-0MVOBJGB4001TQ8X). pytest's
    ``pytest_ignore_collect`` hook only affects *explicit* initial paths, so
    naming the directory on the command line collects it while the default
    recursive run stays unchanged.

    A candidate is ``<root>/.pi/skills/<name>/tests`` or ``.../test`` that
    exists and contains at least one ``*.py`` (recursive, mirroring
    :func:`repo_has_pytest_suite`). Only those two literal path levels are
    probed per skill: no repository-wide scan, and no other hidden root
    (``.claude/``, ``.git/``, ``.worklog/``) is ever traversed.

    Returns:
        Sorted, de-duplicated repo-relative POSIX directory paths. Empty when
        the project has no project-local skill tests.
    """
    root = Path(project_root or REPO_ROOT).resolve()
    skills_root = root.joinpath(*_HIDDEN_SKILL_TEST_ROOT)
    if not skills_root.is_dir():
        return []
    try:
        skill_dirs = sorted(p for p in skills_root.iterdir() if p.is_dir())
    except OSError:
        return []

    candidates: set[str] = set()
    for skill_dir in skill_dirs:
        for dirname in _HIDDEN_SKILL_TEST_DIRNAMES:
            candidate = skill_dir / dirname
            if not candidate.is_dir():
                continue
            try:
                if not any(candidate.rglob("*.py")):
                    continue
            except OSError:
                continue
            candidates.add(candidate.relative_to(root).as_posix())
    return sorted(candidates)


def hidden_skill_test_command(dirs: list[str]) -> str:
    """One quiet pytest command listing every hidden skill test directory.

    Deliberately a **single** command rather than one per directory (D2): the
    runner acquires one host-wide concurrency slot per suite execution, and a
    single command keeps one cache entry and one timeout budget.

    Args:
        dirs: Repo-relative directory paths, already sorted by
            :func:`hidden_skill_test_dirs`. Paths are shell-quoted, so a
            directory containing spaces is passed intact.
    """
    return canonicalize_quiet_test_command(
        "pytest " + " ".join(shlex.quote(d) for d in dirs)
    )


# ---------------------------------------------------------------------------
# Pinned-suite precedence and no-double-execution (SA-0MVQQ9NV4005UKYZ)
# ---------------------------------------------------------------------------

#: Project pytest configuration files whose ``testpaths``/``addopts`` can
#: already collect a hidden skill test directory (D3 coverage rule).
_PYTEST_INI_FILES = ("pytest.ini", "setup.cfg", "tox.ini")
#: INI sections pytest reads options from.
_PYTEST_INI_SECTIONS = ("pytest", "tool:pytest")
#: The only hidden root whose tokens count as coverage (D-A2): a token at or
#: above the repo root never reaches ``.pi/`` because pytest's default
#: ``norecursedirs`` contains ``.*``.
_HIDDEN_ROOT_PREFIX = ".pi"


def _resolve_repo_relative(root: Path, token: str) -> str | None:
    """Normalise a config token to a repo-relative POSIX path.

    Returns ``None`` for tokens that are not paths (option flags such as
    ``-ra``), or that resolve outside *root*. ``"."`` is returned for the
    repository root itself.
    """
    candidate = token.strip()
    if not candidate or candidate.startswith("-"):
        return None
    path = Path(candidate)
    try:
        if path.is_absolute():
            rel = path.resolve().relative_to(root)
        else:
            normalised = os.path.normpath(candidate)
            if normalised == ".":
                return "."
            if normalised == ".." or normalised.startswith(".." + os.sep):
                return None
            rel = Path(normalised)
    except (OSError, ValueError):
        return None
    return rel.as_posix()


def _pytest_config_path_tokens(project_root: Path) -> list[str]:
    """Path tokens from the project's pytest ``testpaths``/``addopts``.

    Reads the INI forms (``pytest.ini``/``setup.cfg``/``tox.ini``) and the
    ``pyproject.toml`` ``[tool.pytest.ini_options]`` form. Unreadable or
    malformed configuration is treated as absent (never an error) — the
    coverage rule may only ever *add* the hidden skill-test command.
    """
    root = Path(project_root)
    tokens: list[str] = []

    for name in _PYTEST_INI_FILES:
        path = root / name
        if not path.is_file():
            continue
        parser = configparser.ConfigParser(
            inline_comment_prefixes=("#", ";"), strict=False
        )
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError):
            continue
        for section in _PYTEST_INI_SECTIONS:
            if not parser.has_section(section):
                continue
            for option in ("testpaths", "addopts"):
                value = parser.get(section, option, fallback="")
                tokens.extend(str(value).split())

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            data = None
        options = ((data or {}).get("tool") or {}).get("pytest") or {}
        ini_options = (options.get("ini_options") or {}) if isinstance(options, dict) else {}
        if isinstance(ini_options, dict):
            for key in ("testpaths", "addopts"):
                value = ini_options.get(key)
                if isinstance(value, str):
                    tokens.extend(value.split())
                elif isinstance(value, list):
                    tokens.extend(str(item) for item in value)
    return tokens


def _token_covers_candidate(root: Path, token: str, candidate: str) -> bool:
    """Whether *token* already collects *candidate* (D3/D-A2 rule).

    The token must resolve to ``.pi`` or a strict descendant of ``.pi`` — a
    token at or above the repo root (``.``, ``tests``, ``skill``) does not
    count, because the default ``norecursedirs`` (``.*``) still prunes
    ``.pi`` from those roots. The candidate must then be the token itself or
    live underneath it.
    """
    rel = _resolve_repo_relative(root, token)
    if rel is None or rel == ".":
        return False
    if rel != _HIDDEN_ROOT_PREFIX and not rel.startswith(_HIDDEN_ROOT_PREFIX + "/"):
        return False
    return candidate == rel or candidate.startswith(rel.rstrip("/") + "/")


def uncovered_hidden_skill_test_dirs(
    project_root: Path | None = None,
) -> list[str]:
    """Hidden skill test dirs the canonical pytest command does NOT collect.

    Applies the no-double-execution rule (D3/AC4): a candidate already
    reached through the project's pytest ``testpaths``/``addopts`` is dropped
    from the emitted command, so the tests run exactly once.
    """
    root = Path(project_root or REPO_ROOT).resolve()
    tokens = _pytest_config_path_tokens(root)
    return [
        candidate
        for candidate in hidden_skill_test_dirs(root)
        if not any(_token_covers_candidate(root, token, candidate) for token in tokens)
    ]


def _pinned_commands_cover(root: Path, candidate: str, commands: list[str]) -> bool:
    """Whether any pinned ``suiteCommands`` entry already names *candidate*."""
    for command in commands:
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        if any(_token_covers_candidate(root, token, candidate) for token in tokens):
            return True
    return False


def hidden_skill_test_notice(
    project_root: Path | None = None,
) -> dict[str, Any] | None:
    """Machine-readable, **non-fatal** notice for a pinned ``suiteCommands``.

    A project's pinned list is authoritative and is returned verbatim (D3) —
    the framework never appends a command to it. When that list does not
    name some of the project's hidden skill test directories, the gap is
    surfaced here instead of staying silent: consumers report the notice and
    leave the resolution and the exit status untouched (AC3/AC4b).

    Returns:
        ``None`` when the list is not pinned or it covers every candidate;
        otherwise a dict with the stable ``code``, the uncovered
        ``directories`` and a human-readable ``message``.
    """
    root = Path(project_root or REPO_ROOT).resolve()
    pinned = extension_suite_commands(root)
    if pinned is None:
        return None
    uncovered = [
        candidate
        for candidate in hidden_skill_test_dirs(root)
        if not _pinned_commands_cover(root, candidate, pinned)
    ]
    if not uncovered:
        return None
    listed = ", ".join(uncovered)
    return {
        "code": NOTICE_HIDDEN_SKILL_TESTS_UNCOVERED,
        "source": TEST_CONFIG_FILE,
        "directories": uncovered,
        "message": (
            f"Pinned {TEST_CONFIG_FILE} suiteCommands do not cover the hidden "
            f"skill test dir(s) {listed}; they are not run by the pinned "
            "list (add them to suiteCommands to collect them)."
        ),
    }


def full_suite_notices(project_root: Path | None = None) -> list[dict[str, Any]]:
    """Every non-fatal resolution notice for the full suite (ordered).

    The single place consumers read notices from; new notices are appended
    here without changing :func:`full_suite_commands`'s ``list[str]``
    signature (D4).
    """
    notices: list[dict[str, Any]] = []
    hidden_notice = hidden_skill_test_notice(project_root)
    if hidden_notice is not None:
        notices.append(hidden_notice)
    return notices


def _read_test_config(project_root: Path) -> dict | None:
    """Read ``.pi/test-config.json`` under *project_root* (None when absent
    or unreadable — a corrupt file is treated as absent, never an error)."""
    config_path = project_root / TEST_CONFIG_FILE
    if not config_path.is_file():
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def extension_suite_commands(project_root: Path | None = None) -> list[str] | None:
    """The extension file's ``suiteCommands`` list, or None when absent.

    When a valid list is present it is the PRIMARY command list and convention
    detection is skipped (F2 AC1). An explicitly-empty list is honored as-is
    (a project declaring "no runnable suite").
    """
    root = Path(project_root or REPO_ROOT).resolve()
    config = _read_test_config(root)
    if config is None:
        return None
    suite_commands = config.get("suiteCommands")
    if not isinstance(suite_commands, list):
        return None
    if not all(isinstance(c, str) and c.strip() for c in suite_commands):
        return None
    return suite_commands


def suite_timeout_per_command(project_root: Path | None = None) -> int | None:
    """Optional per-command timeout (seconds) from the extension file.

    Returns the ``timeoutPerCommand`` value when present and positive, else
    None (callers fall back to their default per-command timeout).
    """
    root = Path(project_root or REPO_ROOT).resolve()
    config = _read_test_config(root)
    if config is None:
        return None
    timeout = config.get("timeoutPerCommand")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return None
    timeout = int(timeout)
    return timeout if timeout > 0 else None


# ---------------------------------------------------------------------------
# Named test groups (SA-0MUJKF2HP001BR51)
# ---------------------------------------------------------------------------
#: Group definition schema: each group has "commands" (list[str]) and "paths"
#: (list[str] — glob-like patterns that, when matched by changed files, cause
#: this group to be selected).  The special group ``"full"`` is the fallback
#: and is always present.
_GROUP_SCHEMA = {"commands": list, "paths": list}


def _read_groups(project_root: Path | None = None) -> dict[str, dict[str, list[str]]] | None:
    """Return the project's named test groups, or None when absent.

    Reads ``<project_root>/.pi/test-config.json`` → ``groups`` key.  The
    ``full`` group is injected automatically (see :func:`full_group_commands`)
    so callers never need to handle its absence.

    Returns:
        A dict mapping group name → ``{"commands": [...], "paths": [...]}``
        when the config defines groups; ``None`` when absent.
    """
    root = Path(project_root or REPO_ROOT).resolve()
    config = _read_test_config(root)
    if config is None:
        return None
    raw_groups = config.get("groups")
    if raw_groups is None or not isinstance(raw_groups, dict):
        return None
    groups: dict[str, dict[str, list[str]]] = {}
    for name, spec in raw_groups.items():
        if not isinstance(name, str) or not name:
            continue
        if (
            isinstance(spec, dict)
            and spec.get("commands")
            and all(isinstance(c, str) and c.strip() for c in spec["commands"])
            and spec.get("paths")
            and all(isinstance(p, str) and p.strip() for p in spec["paths"])
        ):
            groups[name] = {
                "commands": list(spec["commands"]),
                "paths": list(spec["paths"]),
            }
    return groups if groups else None


def _read_groups_with_full(project_root: Path | None = None) -> dict[str, dict[str, list[str]]] | None:
    """Return named groups with ``full`` injected if missing.

    The ``full`` group always exists implicitly: its commands are resolved
    from :func:`full_suite_commands` at runtime.
    """
    groups = _read_groups(project_root)
    if groups is None:
        return None
    if "full" not in groups:
        # Inject a placeholder; commands are resolved at runtime.
        groups["full"] = {"commands": [], "paths": ["**"]}
    return groups


def group_commands(name: str, project_root: Path | None = None) -> list[str] | None:
    """Return the commands for a named group, or None when the group is unknown.

    For the ``full`` group, commands are resolved from
    :func:`full_suite_commands` at runtime.  Other groups return the commands
    defined in the config.
    """
    groups = _read_groups_with_full(project_root)
    if groups is None:
        return None
    spec = groups.get(name)
    if spec is None:
        return None
    if name == "full":
        return full_suite_commands(project_root)
    return list(spec["commands"])


def list_groups(project_root: Path | None = None) -> list[str] | None:
    """Return the list of available group names, or None when no groups defined.

    Always includes ``full`` when groups are available.
    """
    groups = _read_groups_with_full(project_root)
    if groups is None:
        return None
    return sorted(groups.keys())


def _paths_match_spec(changed_file: str, paths_spec: list[str]) -> bool:
    """Return True when *changed_file* matches any pattern in *paths_spec*.

    Patterns are simple glob-like strings:

    - ``skill/audit/**`` — matches anything under ``skill/audit/``.
    - ``**`` — matches everything.
    - ``skill/test_cache.py`` — exact file match.

    Uses :func:`fnmatch.fnmatchcase` for the matching.
    """
    import fnmatch

    for pattern in paths_spec:
        if fnmatch.fnmatchcase(changed_file, pattern):
            return True
    return False


def select_group_from_changed_files(
    changed_files: set[str] | None = None,
    project_root: Path | None = None,
) -> str | None:
    """Select the most relevant group from changed files.

    Maps touched paths to groups via the ``paths`` spec in each group's
    definition.  The group that covers the **most changed files** wins.
    When multiple groups tie on changed-file count, the one with the
    fewest commands (most specific) breaks the tie.  When no group matches,
    returns ``None`` so the caller falls back to ``full``.

    Args:
        changed_files: Changed file set (default: computed from repo).
        project_root: Project root.

    Returns:
        The selected group name (e.g. ``"audit"``), or ``None`` for the
        ``full`` fallback.
    """
    root = Path(project_root or REPO_ROOT).resolve()
    if changed_files is None:
        changed_files = compute_changed_files(root)
    if not changed_files:
        return None

    groups = _read_groups(root)
    if groups is None:
        return None

    # Count how many changed files match each group's paths spec.
    file_count: dict[str, int] = {}
    # Also track command count for tie-breaking.
    cmd_count: dict[str, int] = {}
    for group_name, spec in groups.items():
        if group_name == "full":
            continue
        cmd_count[group_name] = len(spec["commands"])

    for changed_file in changed_files:
        for group_name, spec in groups.items():
            if group_name == "full":
                continue
            if _paths_match_spec(changed_file, spec["paths"]):
                file_count[group_name] = file_count.get(group_name, 0) + 1

    if not file_count:
        return None

    # Prefer the group covering the most changed files; command count
    # (fewest) breaks ties.
    return min(file_count, key=lambda g: (-file_count[g], cmd_count.get(g, 0)))


def group_scope_commands(
    group_name: str,
    project_root: Path | None = None,
) -> list[str] | None:
    """Return the commands for a named group.

    Returns None when the group is unknown or no groups are defined.
    """
    return group_commands(group_name, project_root)




def _npm_test_command(project_root: Path) -> str | None:
    """The canonical ``npm --silent test`` command when the repo declares a
    package.json ``test`` script, else None (F2 AC2 npm-test convention)."""
    pkg_json = project_root / "package.json"
    if not pkg_json.is_file():
        return None
    try:
        scripts = json.loads(pkg_json.read_text(encoding="utf-8")).get("scripts", {})
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(scripts, dict) or not scripts.get("test"):
        return None
    return canonicalize_quiet_test_command("npm test")


def node_suite_commands(project_root: Path | None = None) -> list[str]:
    """Return one quiet node test command per Node suite directory.

    *project_root* defaults to ``REPO_ROOT``; pass another root to build the
    suite commands for a different project (e.g. a sibling repo targeted by
    the audit skill's read-only full-suite verification).

    Prefers ``npm --silent test`` when the repo has an npm test script;
    otherwise falls back to ``node --test <glob>`` for each suite dir.

    Globs are required (SA-0MSF8KNE3003JDVD): on node v22.22.1 ``node --test
    <dir>`` treats a bare directory argument as a module entry point and fails
    with ``MODULE_NOT_FOUND``; only glob patterns (e.g.
    ``node --test "tests/node/**/*.mjs"``) are scanned as test files.
    """
    root = Path(project_root or REPO_ROOT)
    pkg_json = root / "package.json"
    has_npm_test_script = False
    if pkg_json.exists():
        try:
            import json as _json

            has_npm_test_script = "test" in _json.loads(pkg_json.read_text()).get("scripts", {})
        except Exception:  # noqa: BLE001
            has_npm_test_script = False

    cmds: list[str] = []
    for suite_dir in NODE_SUITE_DIRS:
        if not (root / suite_dir).is_dir():
            # Skip suite dirs that don't exist in this project
            # (SA-0MSJELL44009XYIL): emitting a command for a missing dir
            # yields a guaranteed-failing run (e.g. vitest "No test files
            # found", exit 1) that defeats read-only consumers' fail-closed
            # auto-verification (audit skill) for repos whose layout diverges
            # from NODE_SUITE_DIRS. The framework repo itself has all three
            # dirs, so its command set is unchanged.
            continue
        if has_npm_test_script:
            cmds.append(canonicalize_quiet_test_command(f"npm test -- {suite_dir}"))
        else:
            cmds.append(f'node --test "{suite_dir}/**/*.mjs"')
    return cmds


def full_suite_commands(project_root: Path | None = None) -> list[str]:
    """Return the canonical full-suite command set for *project_root*.

    The set that constitutes "the full project test suite", resolved in order:

    1. **Extension file** (F2 AC1): ``<root>/.pi/test-config.json``
       ``suiteCommands`` — when present, convention detection is skipped.
    2. **pytest** only when the repo declares a pytest suite
       (``repo_has_pytest_suite`` — never a phantom pytest command, F2 AC3).
    3. **project-local hidden skill tests** (SA-0MVQJSGLY0098DVB D2): one
       extra quiet pytest command naming every ``.pi/skills/<name>/tests``
       directory the default recursive run cannot reach (pytest's default
       ``norecursedirs`` prunes ``.*``). Emitted immediately after the
       canonical pytest command and before any node command, so the ordering
       is stable. Emission does NOT require ``repo_has_pytest_suite``: a
       project shipping only skill tests still gets the command.
    4. **node** — one command per existing ``tests/{unit,node,cli}`` dir
       (SA-0MSJELL44009XYIL).
    5. **npm-test convention** (F2 AC2): when neither pytest nor any node
       suite dir applies and ``package.json`` declares a ``test`` script,
       emit ``npm --silent test`` (the TCE fix — a vitest/npm repo's real
       suite becomes cacheable/verifiable instead of the phantom pytest).

    Read-only consumers (e.g. the audit skill's automatic full-suite
    verification, SA-0MSIU5HFI0024D7W) query the per-repo test cache with
    exactly these commands so cache entries written by ``run_tests.py`` are
    reused without executing anything. This function is the single source of
    truth for suite-command resolution (F2 AC4).
    """
    root = Path(project_root or REPO_ROOT).resolve()

    ext = extension_suite_commands(root)
    if ext is not None:
        return ext

    commands: list[str] = []
    if repo_has_pytest_suite(root):
        commands.append(pytest_command())
    # No-double-execution (SA-0MVQQ9NV4005UKYZ AC4): a directory the project's
    # own pytest ``testpaths``/``addopts`` already collects is not repeated.
    hidden_skill_dirs = uncovered_hidden_skill_test_dirs(root)
    if hidden_skill_dirs:
        # Straight after the canonical pytest command, before any node
        # command: a stable position for every consumer (D2).
        commands.append(hidden_skill_test_command(hidden_skill_dirs))
    commands.extend(node_suite_commands(root))

    if not commands:
        npm = _npm_test_command(root)
        if npm is not None:
            commands.append(npm)
    return commands


# ---------------------------------------------------------------------------
# Test-type (profile) resolution
# ---------------------------------------------------------------------------


class TypeResolutionError(RuntimeError):
    """Raised when a requested test type cannot be resolved to commands.

    Covers both unknown types and minimum types that a present local extension
    omits. Callers surface the message and exit non-zero — never silently
    substitute the full suite (SA-0MTJQB2MA008HMO6 AC2/AC4).
    """


class UnknownTestTypeError(TypeResolutionError):
    """Raised when ``--type`` names no known or locally-defined type."""


def local_test_types(project_root: Path | None = None) -> dict[str, list[str]] | None:
    """Return the project's local type→commands map, or None when absent.

    Reads ``<project_root>/.pi/skills_extensions/test/extension.json`` through
    the shared loader (SA-0MSQ7MQEJ0064ZB0). Returns None when there is no
    extension or it defines no ``types`` map (so convention fallback applies).
    A present-but-malformed map raises :class:`SkillExtensionError` naming the
    file — never a silent fallback.
    """
    root = Path(project_root or REPO_ROOT).resolve()
    ext = load_extension(TEST_SKILL_NAME, root)
    if not ext.present:
        return None
    data_path = ext.data_path or (ext.directory / DATA_FILENAME)
    raw_types = ext.data.get("types")
    if raw_types is None:
        # Extension present but no type map (e.g. prose-only hooks): there are
        # no local type definitions, so minimum types use conventions.
        return None
    if not isinstance(raw_types, dict):
        raise SkillExtensionError(
            f"Invalid test type map in {data_path}: 'types' must be a JSON object."
        )
    normalized: dict[str, list[str]] = {}
    for key, value in raw_types.items():
        if not isinstance(key, str) or not key:
            raise SkillExtensionError(
                f"Invalid test type name {key!r} in {data_path}: names must be "
                "non-empty strings."
            )
        if isinstance(value, str):
            commands = [value]
        elif (
            isinstance(value, list)
            and value
            and all(isinstance(item, str) and item.strip() for item in value)
        ):
            commands = list(value)
        else:
            raise SkillExtensionError(
                f"Invalid commands for test type '{key}' in {data_path}: "
                "expected a non-empty string or a non-empty list of strings."
            )
        normalized[key] = commands
    return normalized


def extension_file(project_root: Path | None = None) -> Path:
    """Return the path of the test-skill extension data file."""
    root = Path(project_root or REPO_ROOT).resolve()
    return root / ".pi" / "skills_extensions" / TEST_SKILL_NAME / DATA_FILENAME


def allowed_test_types(project_root: Path | None = None) -> list[str]:
    """Return the sorted allowed type names (minimum set + local types)."""
    local = local_test_types(project_root)
    return sorted(set(MINIMUM_TEST_TYPES) | set(local or {}))


def convention_type_commands(
    project_root: Path | None = None,
    test_type: str = "unit",
) -> list[str]:
    """Return convention-detected commands for *test_type*, if any.

    The convention is a ``tests/<type>/`` directory: pytest when the repo
    declares a pytest suite and the dir holds ``.py`` tests; node when it
    holds ``.mjs`` tests. An empty list means no convention applies (the
    caller then raises a clear diagnostic rather than running the full suite).
    """
    root = Path(project_root or REPO_ROOT).resolve()
    type_dir = root / "tests" / test_type
    if not type_dir.is_dir():
        return []
    commands: list[str] = []
    if repo_has_pytest_suite(root) and any(type_dir.rglob("*.py")):
        commands.append(canonicalize_quiet_test_command(f"pytest tests/{test_type}"))
    if any(type_dir.rglob("*.mjs")):
        commands.append(f'node --test "tests/{test_type}/**/*.mjs"')
    return commands


def resolve_type_commands(
    project_root: Path | None = None,
    test_type: str = DEFAULT_TEST_TYPE,
) -> list[str]:
    """Resolve a test type to its command list.

    Resolution order (AC2/AC3/AC4/AC5):

    1. A locally-defined type (``.pi/skills_extensions/test/extension.json``)
       wins, including extra types beyond the minimum set.
    2. ``full`` always falls back to :func:`full_suite_commands` when the
       local map omits it, so a bare invocation (no ``--type``) preserves the
       existing full-suite contract even when an extension exists (AC5).
    3. A minimum type (``unit``/``smoke``) with a present local map that omits
       it fails with a diagnostic naming the type and the extension file —
       never a silent full-suite substitution (AC2).
    4. A minimum type with no local map falls back to convention detection
       (``tests/<type>``); when no convention applies it fails with a clear
       diagnostic (AC4).
    5. Anything else is an unknown type (AC3).

    Raises:
        UnknownTestTypeError: the type is neither minimum nor locally defined.
        TypeResolutionError: the type cannot be resolved to commands.
        SkillExtensionError: the local extension is malformed.
    """
    root = Path(project_root or REPO_ROOT).resolve()
    local = local_test_types(root)
    if local is not None and test_type in local:
        return list(local[test_type])

    if test_type == DEFAULT_TEST_TYPE:
        return full_suite_commands(root)

    if test_type in MINIMUM_TEST_TYPES:
        if local is not None:
            raise TypeResolutionError(
                f"Local test extension {extension_file(root)} does not define "
                f"the minimum test type '{test_type}' (defined: "
                f"{', '.join(sorted(local))}). Add it to extension.json or "
                "remove the extension to use convention detection."
            )
        commands = convention_type_commands(root, test_type)
        if commands:
            return commands
        raise TypeResolutionError(
            f"No commands resolved for test type '{test_type}': no local test "
            f"extension defines it and no tests/{test_type} convention exists."
        )

    raise UnknownTestTypeError(
        f"Unknown test type '{test_type}': it is not one of the minimum types "
        f"({', '.join(MINIMUM_TEST_TYPES)}) and is not defined by the local "
        f"test extension {extension_file(root)}."
    )


def filter_commands_for_suite(name: str, commands: list[str]) -> list[str]:
    """Filter a type's commands to the named suite (pytest/node/all).

    Raising for empty results is the caller's responsibility so it can add a
    context-specific diagnostic.
    """
    if name == "all":
        return list(commands)
    if name == "pytest":
        return [c for c in commands if "pytest" in c]
    if name == "node":
        return [c for c in commands if "node" in c]
    raise ValueError(f"unknown suite: {name}")


# ---------------------------------------------------------------------------
# Failure parsing
# ---------------------------------------------------------------------------


def parse_pytest_failures(output: str) -> list[dict[str, str]]:
    """Parse pytest ``-r a`` output into per-failure structured records.

    Both ``FAILED`` (test failures) and ``ERROR`` (collection / setup /
    teardown errors) short-summary lines are parsed: an error-only run exits
    non-zero without any ``FAILED`` line, and treating it as an unnamed
    suite-level failure hid the responsible test from triage
    (SA-0MSRN0Q64005YR5A).

    Each record contains ``test_name``, ``stdout_excerpt`` and ``stack_trace``
    in the shape expected by triage check_or_create.py.
    """
    records: list[dict[str, str]] = []
    for match in _RED_SUMMARY_RE.finditer(output):
        test_name = match.group("name").strip()
        message = (match.group("message") or "").strip()
        # Extract the traceback section for this test from the FAILURES or
        # ERRORS block (both use the same ``____ <nodeid> ____`` header).
        stack_trace = _extract_pytest_section(output, test_name)
        excerpt = stack_trace[:1000] if stack_trace else message
        records.append(
            {
                "test_name": test_name,
                "stdout_excerpt": excerpt or test_name,
                "stack_trace": stack_trace or excerpt or test_name,
            }
        )
    return records


def _extract_pytest_section(output: str, test_name: str) -> str:
    """Return the pytest FAILURES/ERRORS section body for a test name.

    The section header is the test node id's tail (function name or full id),
    optionally prefixed for errors — e.g. ``ERROR at setup of test_x`` — so
    the tail is matched both exactly and as a header suffix
    (SA-0MSRN0Q64005YR5A).
    """
    tail = test_name.split("::")[-1]
    lines = output.splitlines()
    section_start = None
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line.strip())
        if not m:
            continue
        header = m.group(1).strip()
        if header == test_name or re.search(
            rf"(?:^|[\s:]){re.escape(tail)}$", header
        ):
            section_start = i
            break
    if section_start is None:
        return ""
    body: list[str] = []
    for line in lines[section_start + 1 :]:
        if _SECTION_RE.match(line.strip()) or line.startswith("====="):
            break
        body.append(line)
    return "\n".join(body).strip()


def parse_node_failures(output: str) -> list[dict[str, str]]:
    """Parse Node TAP output into per-failure structured records."""
    records: list[dict[str, str]] = []
    for match in _NODE_NOT_OK_RE.finditer(output):
        test_name = match.group(1).strip()
        block = _extract_node_block(output, test_name)
        error = _extract_yaml_block(block, "error")
        stack = _extract_yaml_block(block, "stack")
        stack_trace = stack or error or test_name
        excerpt = (error or stack or test_name)[:1000]
        records.append(
            {
                "test_name": test_name,
                "stdout_excerpt": excerpt,
                "stack_trace": stack_trace,
            }
        )
    return records


def _extract_node_block(output: str, test_name: str) -> str:
    """Return the TAP YAML block following the ``not ok`` line for a test."""
    idx = output.find("not ok")
    while idx != -1:
        line_end = output.find("\n", idx)
        if line_end == -1:
            break
        header = output[idx:line_end]
        if test_name in header:
            block_end = output.find("\n  ...", line_end)
            if block_end == -1:
                block_end = output.find("\n1..", line_end)
            return output[line_end : block_end if block_end != -1 else len(output)]
        idx = output.find("not ok", line_end)
    return ""


def _extract_yaml_block(block: str, key: str) -> str:
    """Return the indented YAML block value for ``key: |-`` within a TAP block."""
    marker = f"{key}: |-"
    start = block.find(marker)
    if start == -1:
        return ""
    start += len(marker)
    lines = block[start:].splitlines()
    collected: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not line.startswith("  ") and not line.startswith("\t"):
            break
        collected.append(line)
    return "\n".join(collected).strip()


# Vitest default-reporter markers (MS-0MW3UR8SX009YZWS). Both shapes are
# matched because a real run prints the same failing test twice: once in the
# per-file tree and once in the ``Failed Tests`` block. ANSI colour codes are
# stripped first, and the ASCII transliterations of the tree markers
# (``❯``→``>``, ``×``→``x``) are accepted so a captured or Windows-rendered log
# still parses.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
#: `` FAIL  [|<project>|] <file> > <suite> > <test>`` — the Failed Tests
#: header. The ``|<project>|`` pipe is **optional**: project-mode runs print
#: it, plain node-mode runs print ``FAIL  <file> > <suite> > <test>``
#: (SA-0MWDLPF320060T0H).
_VITEST_FAIL_HEADER_RE = re.compile(
    r"^\s*FAIL\s+(?:\|(?P<project>[^|\n]*)\|\s*)?(?P<name>\S[^\n]*?)\s*$",
    re.MULTILINE,
)
#: `` ❯ [|<project>|] <file> (N tests | M failed) 1234ms`` — the per-file
#: tree header. The ``|<project>|`` pipe is optional (node-mode); the
#: ``(N tests`` parenthetical is required so a ``❯ <file>:<line>:<col>``
#: stack frame is never read as a file header.
_VITEST_FILE_HEADER_RE = re.compile(
    r"^\s*(?:❯|>)\s+(?:\|(?P<project>[^|\n]*)\|\s*)?"
    r"(?P<file>[^\s()]+)\s*\(\s*\d+\s*test",
    re.MULTILINE,
)
#: `` ❯ [<fn>] <file>:<line>:<col>`` — a Vitest stack frame. When the gate's
#: 1200-char output tail has stripped the FAIL header and error line (their
#: message embeds a long diff), the surviving frame plus its source excerpt
#: is the failure's remaining signal — the frame is reported as the failing
#: location (RISE-0MW5JFQCU001YYQK, SA-0MWDLPF320060T0H).
_VITEST_FRAME_RE = re.compile(
    r"^\s*(?:❯|>)\s+(?:\S+\s+)?(?P<loc>\S+:\d+:\d+)\s*$",
    re.MULTILINE,
)
#: The source-window lines printed under a Vitest frame: ``  104|   }``
#: (line + code) and ``      |        ^`` (the caret marker).
_VITEST_EXCERPT_LINE_RE = re.compile(r"^\s*(?:\d+\||\|\s*\^)")
#: ``   × <suite> > <test> 29146ms`` — a failed test inside a file's tree.
_VITEST_TREE_FAIL_RE = re.compile(
    r"^\s*(?P<marker>[×✗x])\s+(?P<name>\S[^\n]*?)"
    r"(?:\s+\d+(?:\.\d+)?\s*(?:ms|s))?\s*$",
    re.MULTILINE,
)
#: Any line that starts a new tree/test entry (used to bound detail capture).
_VITEST_TREE_ENTRY_RE = re.compile(r"^\s*[×✗x✓↓❯>]\s")
#: Lines that end a failure block or a tree section: the heavy rule Vitest
#: prints between blocks, the run summary, and the retry-runner trailer.
_VITEST_BLOCK_END_RE = re.compile(
    r"^\s*(?:⎯{3,}|─{3,}|Test Files\s|Tests\s|Start at\s|Duration\s|\[vitest-runner\])"
)

#: The retry runner's final machine-readable verdict line
#: (``vitest-run-with-retry.ts::runSummary``):
#: ``[vitest-runner] attempts=N status=S outcome=O args="..." expected=N ran=M``
#: (or ``expected=unset`` when no expectation was supplied). Emitted via
#: ``warn`` to stderr after every attempt sequence; a stage's combined output
#: runs up to and including its own verdict line. ``status`` is the numeric
#: attempt-sequence exit code (0/1/124 for a hang); the ``[retry] … Retrying
#: once...`` banner is NOT a verdict line (CG-0MWEHG69O005VM17).
_VITEST_RUNNER_VERDICT_RE = re.compile(
    r"^\[vitest-runner\] attempts=\d+ status=(?P<status>\S+) "
    r"outcome=(?P<outcome>[a-z-]+)",
    re.MULTILINE,
)

#: The retry-runner outcomes that mean the stage's final verdict is GREEN
#: after the retry mechanism ran: ``retry-clean`` (attempt 2 passed) and
#: ``accepted-transient`` (every error block was attributable to an acceptable
#: transient signature). Both require ``status=0`` to accompany them — the
#: verdict line is only trusted when the runner itself reports zero.
_VITEST_GREEN_RETRY_OUTCOMES = frozenset({"retry-clean", "accepted-transient"})


def _split_vitest_runner_segments(
    text: str,
) -> list[tuple[str, tuple[str, str] | None]]:
    """Split *text* into per-stage segments at the retry-runner verdict lines.

    Each ``[vitest-runner]`` line ends the stage it summarises, so a segment
    runs from the end of the previous verdict line (or the start of the
    output) through the end of its own verdict line and carries that verdict
    as ``(status, outcome)``. Text after the LAST verdict line (e.g. a
    tutorial E2E section run by a different script that never emits a verdict
    line) forms a trailing segment with verdict ``None`` — never discarded.

    Returns a list of ``(segment_text, verdict)`` pairs, always non-empty
    (output without any verdict line is one segment with verdict ``None``).
    """
    segments: list[tuple[str, tuple[str, str] | None]] = []
    cursor = 0
    for match in _VITEST_RUNNER_VERDICT_RE.finditer(text):
        segment = text[cursor : match.end()]
        verdict = (match.group("status"), match.group("outcome"))
        segments.append((segment, verdict))
        cursor = match.end()
    tail = text[cursor:]
    if tail:
        segments.append((tail, None))
    if not segments:
        segments.append((text, None))
    return segments


def _is_green_retry_verdict(status: str, outcome: str) -> bool:
    """Whether a runner verdict is the retry-accepted green outcome.

    Only ``status=0`` accompanied by ``retry-clean`` or ``accepted-transient``
    discards a stage's failures. Every other outcome (``clean``, ``failed``,
    ``retry-failed``, ``coverage-failed``, ``hang``) or a non-zero status
    keeps them — a mis-parsed verdict only ever *keeps* failures (fail-safe
    direction, CG-0MWEHG69O005VM17).
    """
    return status == "0" and outcome in _VITEST_GREEN_RETRY_OUTCOMES


def _parse_vitest_with_retry_verdict(
    output: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Parse vitest output honouring the retry runner's per-stage verdicts.

    Segments the combined output at each ``[vitest-runner]`` verdict line and
    parses each stage independently. A stage whose verdict is green
    (``status=0`` + ``retry-clean``/``accepted-transient``) contributes its
    failures to the *discarded* list — the runner already proved the retry
    recovered, so those records are diagnostics, not suite failures. Every
    other stage (non-green verdict, or no verdict at all) contributes its
    failures untouched, so a genuine failure in the same command's output is
    never masked (per-stage segmentation, AC2).

    Returns ``(failures, discarded)`` — the two lists share the record shape
    of :func:`parse_vitest_failures`.
    """
    text = _ANSI_ESCAPE_RE.sub("", output)
    failures: list[dict[str, str]] = []
    discarded: list[dict[str, str]] = []
    for segment, verdict in _split_vitest_runner_segments(text):
        records = parse_vitest_failures(segment)
        if verdict is not None and _is_green_retry_verdict(*verdict):
            discarded.extend(records)
        else:
            failures.extend(records)
    return failures, discarded


def parse_vitest_failures(output: str) -> list[dict[str, str]]:
    """Parse Vitest's default-reporter output into structured failure records.

    Vitest names a failing test after its file and suite — ``FAIL  [|<project>|]
    <file> > <suite> > <test>`` in the ``Failed Tests`` block (the ``|<project>|``
    pipe is optional: node-mode runs omit it) and ``× <suite> > <test>`` under
    the ``❯ [|<project>|] <file>`` tree header. Before
    MS-0MW3UR8SX009YZWS the runner understood only TAP, so every Vitest
    failure produced ``failures: []`` and was classified ``unparsed_failure``
    (gate ``reason: unparsed``): the operator was told the *command or
    configuration* was broken when the *code* was at fault, and triage fell
    back to a ``<suite exited N>`` placeholder instead of the test name.

    The ``Failed Tests`` block is parsed first (it carries the error, code
    frame and stack), then the per-file tree; a test reported in both places
    yields **one** record. When neither pass finds anything — the gate's
    1200-char output tail (``GATE_OUTPUT_TAIL_CHARS``) can strip the FAIL
    header and tree markers, leaving only the ``❯ <file>:<line>:<col>``
    frames plus their source excerpts — the frames themselves become the
    failing locations. Summary lines (``Test Files …``/``Tests …``) never
    produce a record, and output without any of these markers yields none —
    an infrastructure failure (``ENOSPC`` in the browser provider) therefore
    keeps its ``unparsed_failure`` classification.

    Args:
        output: The command's combined stdout/stderr.

    Returns:
        One record per failing test with ``test_name`` (file, suite and test),
        ``stdout_excerpt`` (≤1000 chars) and ``stack_trace`` — the shape triage
        ``check_or_create.py`` and the audit consume.
    """
    text = _ANSI_ESCAPE_RE.sub("", output)
    records: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(name: str, detail: str) -> None:
        clean_name = " ".join(name.split())
        key = clean_name.lower()
        if not key or key in seen:
            return
        seen.add(key)
        body = detail.strip()
        records.append(
            {
                "test_name": clean_name,
                "stdout_excerpt": (body or clean_name)[:1000],
                "stack_trace": body or clean_name,
            }
        )

    # 1) Failed Tests blocks — the richest attribution (error + stack).
    headers = list(_VITEST_FAIL_HEADER_RE.finditer(text))
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        add(header.group("name"), _vitest_block_body(text[header.end() : end]))

    # 2) Per-file tree markers — the only attribution when a log is truncated
    #    before the Failed Tests block.
    lines = text.splitlines()
    current_file = ""
    for index, line in enumerate(lines):
        file_match = _VITEST_FILE_HEADER_RE.match(line)
        if file_match:
            current_file = file_match.group("file")
            continue
        fail_match = _VITEST_TREE_FAIL_RE.match(line)
        if not fail_match or not current_file:
            continue
        detail = _vitest_tree_detail(lines, index + 1)
        add(f"{current_file} > {fail_match.group('name')}", detail)

    # 3) Frame-only fallback (SA-0MWDLPF320060T0H): when the gate's 1200-char
    #    output tail has stripped both the FAIL headers and the tree markers,
    #    the ``❯ <file>:<line>:<col>`` frames (plus source excerpts) are the
    #    surviving failure signal — e.g. an assertion whose message embeds a
    #    long README diff. Each frame (or the excerpt under it) becomes a
    #    structured record whose failing location is the frame. Only reached
    #    when the richer passes found nothing, so a full output never
    #    double-reports a failure's stack frames as extra records.
    if not records:
        for index, line in enumerate(lines):
            frame = _VITEST_FRAME_RE.match(line)
            if not frame:
                continue
            loc = frame.group("loc")
            # The frame line itself heads the record's detail so the failing
            # location is visible in both test_name and stack_trace, followed
            # by the source excerpt when one is printed (SA-0MWDLP4SW0099MZM
            # contract: the captured frame must appear in stack_trace).
            excerpt = _vitest_frame_detail(lines, index + 1)
            detail = line.strip() + ("\n" + excerpt if excerpt else "")
            add(loc, detail)

    return records


def _vitest_block_body(block: str) -> str:
    """Return the error/code-frame/stack lines that follow a FAIL header."""
    body: list[str] = []
    for line in block.splitlines():
        if _VITEST_BLOCK_END_RE.match(line):
            break
        body.append(line)
    return "\n".join(body).strip()


def _vitest_tree_detail(lines: list[str], start: int) -> str:
    """Return the ``→ <message>`` line(s) printed under a tree failure marker."""
    detail: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            if detail:
                break
            continue
        if _VITEST_BLOCK_END_RE.match(line) or _VITEST_TREE_ENTRY_RE.match(line):
            break
        detail.append(line.strip())
    return "\n".join(detail).strip()


def _vitest_frame_detail(lines: list[str], start: int) -> str:
    """Return the source-excerpt lines printed under a Vitest frame line.

    Vitest prints the failing source window as ``N| code`` lines followed by a
    `` | ^`` caret marker; collection stops at the first non-excerpt line (the
    next frame, a blank line or a block/summary marker). The frame location
    itself is not repeated — it is already the record's ``test_name``.
    """
    detail: list[str] = []
    for line in lines[start:]:
        if _VITEST_EXCERPT_LINE_RE.match(line):
            detail.append(line.strip())
            continue
        break
    return "\n".join(detail).strip()


def parse_failures(command: str, output: str) -> list[dict[str, str]]:
    """Parse *output* with the failure parser matching *command*'s reporter.

    Routing is shared by every consumer (``run_suite`` and the audit's suite
    loop) so the two can never drift apart:

    * ``pytest`` commands → :func:`parse_pytest_failures` (unchanged);
    * Vitest commands → :func:`parse_vitest_failures`, with the retry
      runner's per-stage verdicts honoured (see
      :func:`_parse_vitest_with_retry_verdict`);
    * anything else → :func:`parse_node_failures` (TAP, unchanged), falling
      back to the retry-aware Vitest parser when TAP yields nothing. The
      fallback covers wrapper commands (``npm --silent test``) whose output
      shape cannot be inferred from the command line; the Vitest markers are
      specific enough that non-Vitest output still yields no records.
    """
    if "pytest" in command:
        return parse_pytest_failures(output)
    if is_vitest_command(command):
        return _parse_vitest_with_retry_verdict(output)[0]
    records = parse_node_failures(output)
    return records or _parse_vitest_with_retry_verdict(output)[0]


def parse_failures_with_retry_verdict(
    command: str, output: str
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Shared :func:`parse_failures` plus the discarded retry-recovered records.

    ``run_suite`` uses this so a green-verdict stage's discarded failures can
    be reported as diagnostics (a stable notice / ``discarded_failures`` field)
    without becoming suite failures; ``parse_failures`` delegates to it and
    returns only the genuine failures. Both lists share the record shape of
    :func:`parse_vitest_failures`.

    Routing is identical to :func:`parse_failures`:

    * ``pytest`` commands → :func:`parse_pytest_failures` (no discarded list);
    * Vitest commands → :func:`_parse_vitest_with_retry_verdict`;
    * anything else → TAP, falling back to the retry-aware Vitest parser.
    """
    if "pytest" in command:
        return parse_pytest_failures(output), []
    if is_vitest_command(command):
        return _parse_vitest_with_retry_verdict(output)
    records = parse_node_failures(output)
    if records:
        return records, []
    return _parse_vitest_with_retry_verdict(output)


# ---------------------------------------------------------------------------
# Suite execution
# ---------------------------------------------------------------------------


class TestConcurrencyTimeout(RuntimeError):
    """Raised when a test run cannot acquire a concurrency slot in time.

    Bounded-wait semantics (SA-0MTG5U75A001F1RG AC1): the shared "test"
    semaphore is acquired with ``TEST_LOCK_TIMEOUT`` (default 600s); when
    the ceiling stays saturated past the deadline the run reports a clear
    failure notice instead of failing fast or hanging. Cache hits never
    execute, so they never acquire a slot and never raise
    (SA-0MTG5U75A001F1RG AC4).
    """

    __test__ = False  # suppress pytest collection warning


def _test_semaphore_max_workers() -> int:
    """Resolve the FULL-suite concurrency ceiling.

    Precedence: ``TEST_MAX_CONCURRENCY`` env var > calibrated default 6. Values
    below 1 are clamped to 1; an invalid (non-integer) value is ignored with a
    warning so a misconfigured environment cannot break test runs. The ceiling
    bounds concurrent full-suite executions host-wide via the shared "test"
    semaphore — a separate namespace from both the audit semaphore and the
    partial-run "test-fast" lane (SA-0MTG5U75A001F1RG AC1/AC2/AC3,
    SA-0MVWZQ269009SJXV AC2).
    """
    return _resolve_ceiling(TEST_MAX_CONCURRENCY_ENV, TEST_MAX_CONCURRENCY_DEFAULT)


def _test_fast_semaphore_max_workers() -> int:
    """Resolve the PARTIAL-run ("test-fast" lane) concurrency ceiling.

    Precedence: ``TEST_MAX_CONCURRENCY_FAST`` env var > default 4, with the
    same clamping and invalid-value handling as the full-suite ceiling
    (:func:`_test_semaphore_max_workers`). Partial runs are short, so a modest
    pool absorbs a burst of concurrent agents without ever waiting behind a
    full suite (SA-0MVWZQ269009SJXV AC1).
    """
    return _resolve_ceiling(
        TEST_MAX_CONCURRENCY_FAST_ENV, TEST_MAX_CONCURRENCY_FAST_DEFAULT
    )


def _resolve_ceiling(env_name: str, default: int) -> int:
    """Return the env-configured ceiling, or *default* when unset/invalid.

    Shared by both lanes so clamping (< 1 -> 1) and the invalid-value warning
    are implemented exactly once.
    """
    env_value = os.environ.get(env_name)
    if env_value:
        try:
            return max(1, int(env_value))
        except ValueError:
            print(
                f"Warning: invalid {env_name} value "
                f"{env_value!r}; using default ceiling",
                file=sys.stderr,
            )
    return default


def slot_lane_for(scope: str, test_type: str) -> str:
    """Return the semaphore lane a run belongs to.

    A run is *partial* — and therefore belongs to the "test-fast" lane —
    whenever it does not select the whole suite: ``scope`` other than
    ``"full"`` (i.e. ``"changed"``) or a ``test_type`` other than ``full``
    (``unit``, ``smoke``, ``group:<name>``). Everything else is a full suite
    and keeps the "test" lane. Callers must pass the **resolved** scope (after
    a changed-scope run falls back to the full suite, the lane is "full"), so
    a lane label always matches what actually executed
    (SA-0MVWZQ269009SJXV AC1/AC4).
    """
    if scope != "full" or test_type != DEFAULT_TEST_TYPE:
        return TEST_SLOT_LANE_FAST
    return TEST_SLOT_LANE_FULL


#: Lane of the slot acquisitions happening in this execution context. Kept as
#: ambient state (rather than a parameter on every call) so a caller can wrap a
#: whole execution in :func:`_slot_lane` and every nested acquisition inherits
#: it — and so existing zero-argument test doubles of
#: :func:`_test_concurrency_slot` keep working.
_SLOT_LANE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "test_slot_lane", default=TEST_SLOT_LANE_FULL
)


@contextmanager
def _slot_lane(lane: str) -> Iterator[None]:
    """Run nested slot acquisitions in *lane* (``"full"`` or ``"fast"``).

    Sets the ambient lane for the duration of the block; an explicit ``lane=``
    argument to :func:`_test_concurrency_slot` still wins, so a caller that
    needs a one-off lane does not have to wrap the execution.
    """
    token = _SLOT_LANE.set(lane)
    try:
        yield
    finally:
        _SLOT_LANE.reset(token)


def _lane_semaphore_config(lane: str) -> tuple[str, str, int]:
    """Return ``(lane, semaphore_name, max_workers)`` for *lane*.

    An unknown lane name is treated as ``"full"``: an unexpected value must
    degrade to the conservative pool, never to an unbounded one.
    """
    if lane == TEST_SLOT_LANE_FAST:
        return (
            TEST_SLOT_LANE_FAST,
            TEST_FAST_SEMAPHORE_NAME,
            _test_fast_semaphore_max_workers(),
        )
    return TEST_SLOT_LANE_FULL, TEST_SEMAPHORE_NAME, _test_semaphore_max_workers()



def _test_lock_timeout() -> float:
    """Resolve the bounded wait for a free test-run slot.

    Precedence: ``TEST_LOCK_TIMEOUT`` env var > default 600s. Unlike the
    audit's fail-fast slot wait, a test run under saturation WAITS
    (bounded) for a free slot instead of failing immediately — the same
    no-fail-fast philosophy as the audit priority queue
    (SA-0MTG5U75A001F1RG AC1).
    """
    env_value = os.environ.get(TEST_LOCK_TIMEOUT_ENV)
    if env_value:
        try:
            return float(env_value)
        except ValueError:
            print(
                f"Warning: invalid {TEST_LOCK_TIMEOUT_ENV} value "
                f"{env_value!r}; using default lock timeout",
                file=sys.stderr,
            )
    return TEST_LOCK_TIMEOUT_DEFAULT


def _test_lock_jitter() -> float:
    """Resolve the per-waiter deadline-stagger fraction.

    Precedence: ``TEST_LOCK_JITTER`` env var > default 0.1. A value is
    clamped to ``[0, 1)`` (an invalid value is ignored with a warning). The
    jitter spreads concurrent waiters' deadlines within the
    ``TEST_LOCK_TIMEOUT`` bound so a herd does not time out — and retry —
    in lockstep (SA-0MVEDOF7A004TSWD AC1/AC2). ``0`` disables the stagger
    (restoring exact historical behaviour); the configured bound is never
    extended, only staggered earlier.
    """
    env_value = os.environ.get(TEST_LOCK_JITTER_ENV)
    if env_value:
        try:
            value = float(env_value)
        except ValueError:
            print(
                f"Warning: invalid {TEST_LOCK_JITTER_ENV} value "
                f"{env_value!r}; using default jitter",
                file=sys.stderr,
            )
            return TEST_LOCK_JITTER_DEFAULT
        if 0.0 <= value < 1.0:
            return value
        print(
            f"Warning: {TEST_LOCK_JITTER_ENV} value {value!r} out of range "
            "[0, 1); using default jitter",
            file=sys.stderr,
        )
    return TEST_LOCK_JITTER_DEFAULT


def _test_vitest_max_workers() -> int:
    """Resolve the per-suite vitest worker-pool bound.

    Precedence: ``VITEST_MAX_WORKERS`` env var > default 4, mirroring the
    host-wide ``TEST_MAX_CONCURRENCY`` contract. Values below 1 are clamped
    to 1 (serial — a zero/negative pool would mean "unbounded" to vitest);
    an invalid (non-integer) value is ignored with a warning so a
    misconfigured environment cannot break test runs.

    The resolved value is exported to every suite-command child environment
    (see :func:`_suite_command_env`), which is how a single vitest suite is
    kept from fanning out to the whole CPU count (SA-0MVE98T1M006A17T).
    """
    env_value = os.environ.get(TEST_VITEST_MAX_WORKERS_ENV)
    if env_value:
        try:
            return max(1, int(env_value))
        except ValueError:
            print(
                f"Warning: invalid {TEST_VITEST_MAX_WORKERS_ENV} value "
                f"{env_value!r}; using default worker cap",
                file=sys.stderr,
            )
    return TEST_VITEST_MAX_WORKERS_DEFAULT


@contextmanager
def _test_concurrency_slot(lane: str | None = None) -> Iterator[None]:
    """Hold a shared test-run slot for the duration of an actual execution.

    Bounds concurrent test suite executions via a shared semaphore, in the
    lane named by *lane* — or, when omitted, the ambient lane set by
    :func:`_slot_lane` (SA-0MVWZQ269009SJXV AC1):

    - ``"full"`` (default) — the ``"test"`` namespace, ceiling
      ``TEST_MAX_CONCURRENCY``, for whole-suite executions.
    - ``"fast"`` — the ``"test-fast"`` namespace, ceiling
      ``TEST_MAX_CONCURRENCY_FAST``, for partial runs (``--scope changed``,
      ``--type unit|smoke|group:*``). Independent of the full-suite pool, so a
      1-3 s partial run never queues behind a 7-minute full suite.

    Both lanes use the bounded wait ``TEST_LOCK_TIMEOUT``. Only ACTUAL
    executions acquire a slot — a cached result is served without executing
    and never blocks on the semaphore, so cache behavior is unchanged
    (SA-0MTG5U75A001F1RG AC4).

    Nested-run isolation (SA-0MUY6U05F008RG86): fixtures that spawn a nested
    ``run_tests.py`` (or call the runner in-process) set ``PI_SEMAPHORE_DIR``
    to a per-test isolated directory via :func:`nested_semaphore_dir`, so the
    nested acquire never waits behind the host-wide slot its outer run holds.
    This context manager itself is namespace-agnostic — it honours whatever
    ``PI_SEMAPHORE_DIR`` resolves to — so a top-level run keeps the host-wide
    ceiling while a nested run is isolated by its fixture.

    Raises:
        TestConcurrencyTimeout: when no slot frees within the bound.
    """
    lane, semaphore_name, max_workers = _lane_semaphore_config(
        lane if lane is not None else _SLOT_LANE.get()
    )
    lock_timeout = _test_lock_timeout()
    ceiling_env = (
        TEST_MAX_CONCURRENCY_FAST_ENV
        if lane == TEST_SLOT_LANE_FAST
        else TEST_MAX_CONCURRENCY_ENV
    )
    sem = Semaphore(
        semaphore_name,
        max_workers=max_workers,
        timeout=lock_timeout,
        # Per-waiter stagger within the bound so a herd of concurrent
        # waiters desynchronises instead of timing out (and retrying) at
        # one shared instant (SA-0MVEDOF7A004TSWD).
        jitter=_test_lock_jitter(),
    )
    try:
        with sem:
            yield
    except TimeoutError as exc:
        raise TestConcurrencyTimeout(
            f"{SLOT_BUSY_NOTICE_PREFIX} no free slot within "
            f"{lock_timeout:.0f}s (lane={lane}, {ceiling_env}={max_workers}) "
            f"— wait a staggered delay before retrying (do not re-run "
            f"immediately; concurrent waiters re-enter the pool in lockstep "
            f"otherwise), or raise TEST_LOCK_TIMEOUT to wait longer."
        ) from exc


def _suite_command_env() -> dict[str, str]:
    """Return the child environment for a suite command.

    Layers, in order:

    1. The shared repository-override scrub + ``~/.local/bin`` PATH injection
       (:func:`test_runner.subprocess_env_with_scrubbed_overrides`) — a leaked
       ``GIT_DIR`` must not redirect real-git fixtures at the live checkout
       (F2, SA-0MUIA3OE40001QJX).
    2. The resolved per-suite vitest worker bound as ``VITEST_MAX_WORKERS``
       (SA-0MVE98T1M006A17T). An ambient value wins (it is re-exported
       normalised); otherwise the calibrated default applies. This is the
       only mutation, and it never leaks back into ``os.environ``.
    3. A disk-backed ``TMPDIR`` resolved by the shared helper
       (:func:`shared.test_tmpdir.resolve_test_tmpdir`, F1
       SA-0MW3EN72O005BMIP) so a suite subprocess never exhausts a tiny
       tmpfs ``/tmp`` inode budget (SA-0MW2QED9M005FWOK). Resolved once per
       process (module-level cache); fail-open — any resolver error is
       logged and the run proceeds with the ambient TMPDIR
       (SA-0MW3ENF32006BGN1 AC3).

    ``NODE_OPTIONS`` and every other unrelated variable are preserved, so
    consumer-provided vitest/NODE_OPTIONS plumbing keeps working. The
    TMPDIR/VITEST changes are env-only — command strings and therefore cache
    keys are byte-identical (SA-0MW3ENF32006BGN1 AC4).
    """
    env = subprocess_env_with_scrubbed_overrides()
    env[TEST_VITEST_MAX_WORKERS_ENV] = str(_test_vitest_max_workers())
    env["TMPDIR"] = _resolved_test_tmpdir()
    return env


#: Module-level cache for the disk-backed test TMPDIR (resolved once per
#: process on first use; SA-0MW3ENF32006BGN1). ``None`` means "not yet
#: resolved".
_TEST_TMPDIR_CACHE: str | None = None


def _resolved_test_tmpdir() -> str:
    """Return the disk-backed test TMPDIR, resolving it once per process.

    Delegates to the shared F1 helper (:func:`shared.test_tmpdir.resolve_test_tmpdir`)
    which picks an explicit ``PI_TEST_TMPDIR`` override, else a disk-backed
    candidate under the configured root, else ``tempfile.gettempdir()`` — and
    never raises. The result is cached module-wide so every suite command of
    a run shares one TMPDIR (resolving repeatedly for each spawned command
    would be wasteful and could pick a different dir mid-run).

    Fail-open (AC3): the F1 helper has a documented never-raise contract, but
    a defensive guard mirrors the same contract here — if resolution ever
    does raise (e.g. a patched/test stub), the error is logged and the
    ambient ``TMPDIR`` (or the portable fallback) is used instead, so a
    TMPDIR problem can never change the run's exit status, cache entry or
    scope resolution.

    Operator guidance (precedence, prune rules, failure signature, remedy):
    ``docs/dev/test-skill-reference.md`` §3 (SA-0MW3ENVLU0097JRD).
    """
    global _TEST_TMPDIR_CACHE
    if _TEST_TMPDIR_CACHE is None:
        try:
            path, reason = resolve_test_tmpdir()
        except Exception as exc:  # noqa: BLE001 - documented fail-open contract
            LOG.warning(
                "Test TMPDIR resolution failed (%s); falling back to ambient TMPDIR",
                exc,
            )
            _TEST_TMPDIR_CACHE = os.environ.get("TMPDIR") or tempfile.gettempdir()
            return _TEST_TMPDIR_CACHE
        _TEST_TMPDIR_CACHE = str(path)
        # AC5: INFO when a disk-backed (or explicit-override) dir is chosen;
        # WARNING when the resolver had to fall back because every candidate
        # was unusable.
        if "disk-backed candidate" in reason or "explicit" in reason:
            LOG.info("Test TMPDIR=%s (%s)", path, reason)
        else:
            LOG.warning(
                "Test TMPDIR candidate unusable; using %s (%s)", path, reason
            )
    return _TEST_TMPDIR_CACHE


def _run_cmd(cmd: list[str], cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess:
    """Run a suite command capturing stdout/stderr.

    The environment is the shared repository-override scrub (F2,
    SA-0MUIA3OE40001QJX): passing an explicit ``env=`` replaces the previous
    bare inherit of the parent environment, so a leaked ``GIT_DIR`` cannot
    redirect real-git test fixtures at the live checkout. It additionally
    carries the per-suite ``VITEST_MAX_WORKERS`` bound
    (SA-0MVE98T1M006A17T) — see :func:`_suite_command_env`.
    """
    log_repository_override_scrub()
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_suite_command_env(),
    )


def _run_suite_command(command: str, cwd: str, timeout: int) -> subprocess.CompletedProcess:
    """Spawn one suite command, with no concurrency-slot handling.

    Uses shlex.split (not str.split) so quoted args like
    ``node --test "tests/node/**/*.mjs"`` pass the glob without literal
    quotes — otherwise node matches zero files and the suite trivially
    "passes" with 0 tests (pre-existing bug fixed with SA-0MSGN5OJ4002OZKY).

    The pytest executable is resolved before spawning so the suite runs under
    the runner's interpreter (or the first probed candidate that can run
    pytest) instead of a PATH ``pytest`` bound to a foreign interpreter
    (SA-0MVK154IE007OS3D); the canonical command (cache key) is unchanged.

    Slot acquisition is deliberately left to the caller: :func:`_cached_runner`
    acquires it directly for legacy callers, while :func:`run_suite` routes
    through :func:`paced_runner` so the queue wait can be reported separately
    (SA-0MVU8XRBQ000DG9D).
    """
    executable = executable_test_command(command)
    return _run_cmd(shlex.split(executable), cwd=Path(cwd), timeout=timeout)


def _cached_runner(command: str, cwd: str, timeout: int) -> subprocess.CompletedProcess:
    """Adapter exposing slot-bounded execution to the cache runner protocol."""
    with _test_concurrency_slot():
        return _run_suite_command(command, cwd, timeout)


def _timed_suite_runner(elapsed_samples: list[float]) -> Runner:
    """Wrap :func:`_run_suite_command` so each execution's wall time is recorded.

    The sample is appended in a ``finally`` block, so a ``TimeoutExpired``
    still reports how long the command had been running when it stopped — the
    fact an operator needs to tell a slow suite from an endlessly hanging one.
    Only the command execution is timed: the queue wait for a host test slot
    happens in the pacer *outside* this runner, so ``elapsed_seconds`` and
    ``slot_wait_seconds`` can never double-count (SA-0MVU8XRBQ000DG9D).
    """

    def _runner(command: str, cwd: str, timeout: int) -> subprocess.CompletedProcess:
        started = time.monotonic()
        try:
            return _run_suite_command(command, cwd, timeout)
        finally:
            elapsed_samples.append(time.monotonic() - started)

    return _runner


def paced_runner(
    runner: Runner | None = None,
    on_wait: Callable[[float, float], None] | None = None,
    lane: str = TEST_SLOT_LANE_FULL,
) -> Runner:
    """Wrap a cache *runner* so each real execution holds a test-run slot.

    The ``run_cached`` protocol invokes *runner* only on a cache **miss**;
    a cache hit returns the stored result without calling it. Wrapping the
    runner — rather than ``run_cached`` itself — therefore gives exactly the
    intended semantics: every real suite execution acquires the shared
    ``"test"`` semaphore (ceiling ``TEST_MAX_CONCURRENCY``, bounded wait
    ``TEST_LOCK_TIMEOUT``), and cache hits never consume a slot.

    *runner* defaults to ``test_cache._default_runner`` (the same runner
    ``run_cached`` uses when none is supplied), so callers that execute a
    command through the cache with default semantics — e.g. the audit's F3
    auto-execution (SA-0MUJK94QN0015925) — can route it through the pacer by
    passing ``runner=paced_runner()`` alone.

    *on_wait*, when supplied, is called with ``(queued_at, wait_seconds)``
    immediately after the slot is acquired and before the command is spawned,
    so callers can emit pacing telemetry (SA-0MUA8BSAG000YZA2 AC3).

    *lane* selects the semaphore namespace: ``"full"`` (default) for a whole
    suite, ``"fast"`` for a partial run (``--scope changed``, ``--type`` other
    than ``full``). The two pools are independent, so a partial run never
    queues behind a full suite. An unknown lane degrades to ``"full"``
    (SA-0MVWZQ269009SJXV AC1).

    This is the single pacer entry point other code paths (e.g.
    ``implement.py``'s finish-gate suites, SA-0MUKHCO02009EQFG) use to route
    their executions through the same host-wide namespace as
    ``run_tests.py`` (SA-0MTG5U75A001F1RG). A caller that does not pace keeps
    its previous unpaced behaviour, so this is opt-in.

    Raises:
        TestConcurrencyTimeout: when no slot frees within ``TEST_LOCK_TIMEOUT``.
    """
    if runner is None:
        runner = _default_runner

    def _paced(command: str, cwd: str, timeout: int) -> subprocess.CompletedProcess:
        queued_at = time.time()
        with _slot_lane(lane), _test_concurrency_slot():
            if on_wait is not None:
                on_wait(queued_at, time.time() - queued_at)
            return runner(command, cwd, timeout)

    return _paced


def _cache_entry_metadata(
    run: dict[str, Any], command: str
) -> dict[str, Any]:
    """Reportable metadata of the cache entry a verdict was served from.

    ``run`` is a ``run_cached`` result (a cache hit). The returned mapping
    carries the entry's ``command``, ``completed_at`` (epoch float),
    ``cache_age_seconds`` (its age at reporting time), ``git_state`` (the
    fingerprint it is keyed on), ``scope`` and ``fingerprint_kind`` so a
    consumer — the finish gate's report, a completion comment, an auditor —
    can state *what* was reused rather than only that something was
    (SA-0MVQHVJEX0071R7G, SA-0MW7C6BMZ00931JG).
    """
    return {
        "command": run.get("command") or command,
        "completed_at": run.get("completed_at", 0),
        "cache_age_seconds": run.get("cache_age_seconds"),
        "git_state": run.get("git_state", ""),
        "scope": run.get("scope", "full"),
        "fingerprint_kind": run.get("fingerprint_kind", ""),
    }


def run_suite(
    name: str,
    cwd: Path | None = None,
    timeout: int = 600,
    use_cache: bool = True,
    force: bool = False,
    no_cache: bool = False,
    commands: list[str] | None = None,
    scope: str = "full",
    base_ref: str = "origin/dev",
    test_type: str = DEFAULT_TEST_TYPE,
    group: str | None = None,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Run a single named suite and return structured results.

    By default each command is routed through the per-repo cache: a valid
    cached result (same git state, within TTL) is served without executing.
    ``force`` bypasses lookup (still stores), ``no_cache`` bypasses lookup
    and storage. The result carries ``cached`` (True when every command in
    the suite was served from cache) and ``command`` for display.

    *commands* (F2 AC4) overrides name-based resolution: ``name == "all"``
    resolves the repo's full suite via ``full_suite_commands(cwd)`` — the
    single source of truth — instead of the hardcoded pytest+node pair. The
    failure parser is chosen per command (pytest commands → pytest parser,
    everything else → node parser), mirroring the audit's
    ``_run_tests_via_test_skill``. Parsing is retry-verdict aware
    (CG-0MWEHG69O005VM17): failures from a stage the retry runner accepted
    (``status=0`` + ``retry-clean``/``accepted-transient``) are carried on the
    result's additive ``discarded_failures`` field plus a stable
    ``notice``/``notice_code`` (``retry_discarded_failures``) — never in
    ``failures`` and never changing ``success``/the exit status.

    *scope* ("full" or "changed") selects which tests run: ``full`` resolves
    the repo's full suite (existing behavior); ``changed`` resolves only
    tests touching files edited on this branch (via
    :func:`changed_scope_commands`), filtering the selection to the named
    suite (pytest commands for ``name="pytest"``, node commands for
    ``name="node"``, everything for ``name="all"``). When ``changed``
    cannot produce a subset for this suite (custom suite commands, no
    selection, no applicable commands), it falls back to the full suite and
    reports ``scope: "full"`` in the result so consumers never mistake a
    partial run for a full one. The fallback resolves through
    ``full_suite_commands(cwd)`` filtered by suite name, so a repo's pinned
    ``suiteCommands`` (custom interpreter) are honoured for every suite name
    (OSL-0MVNWYRQB0095WXQ); when the pinned set defines no command for the
    suite, it raises the unknown-suite-style error. The result always carries
    ``scope``
    ("full"|"changed") and, for changed scope, ``changed_files`` (the file
    set that drove selection). The scope is recorded in the per-repo test
    cache metadata (``run_cached(scope=...)``) so read-only consumers can
    reject partial runs as full-suite evidence.

    *test_type* selects the command profile (``full`` default, ``unit``,
    ``smoke``, or a locally-defined type). It is orthogonal to *scope*:
    ``test_type`` chooses the profile, *scope* chooses full-vs-changed within
    it. Only ``full`` populates the audit-accepted full-suite cache entry;
    other types use independent cache keys and record ``type`` in the result.

    *group* selects a named test group from ``.pi/test-config.json``. When
    provided, the group's commands override *name*-based and *test_type*-based
    resolution (``commands`` is set to the group's commands). The group name
    is recorded in ``type`` as ``"group:<name>"`` so cache keys remain distinct
    (SA-0MUJKF2HP001BR51).

    *fingerprint* (default ``None``) is an explicit expected fingerprint —
    normally a would-be-commit tree SHA from :func:`compute_tree_state` —
    forwarded unchanged to :func:`run_cached`. When supplied it keys the
    lookup and the stored entry (``fingerprint_kind="tree"``) instead of the
    loose worktree state; a stored entry of a different kind or value is a
    miss, so a tree request with no matching entry executes fresh. Callers
    that pass nothing keep the historical loose-worktree keys byte for byte.
    The CLI's ``--fingerprint`` is the fail-closed surface for this parameter
    (an unusable value or a mode that cannot bind it is rejected with exit 2,
    never degraded to a worktree key — SA-0MW7C6AYO0062GJB).

    Returns a dict with ``success``, ``returncode``, ``failures``, ``command``,
    ``cached``, ``scope``, ``type``; on a failure WITHOUT structured failures a
    ``notice`` (human-readable) plus a stable ``notice_code`` and bounded
    ``stdout``/``stderr`` tails (WL-0MVGUT787000LNDG).

    Diagnostic facts (SA-0MVU8XRBQ000DG9D), present on **every** result so a
    consumer never infers what happened:

    * ``configured_timeout`` — the per-command timeout that was applied
      (seconds).
    * ``elapsed_seconds`` — wall time of the command executions only, queue
      wait excluded; ``None`` (never ``0``) when nothing executed (a fully
      cache-served suite, or a slot wait that expired before spawning).
    * ``slot_wait_seconds`` — time spent queued for a host test slot, measured
      once by the pacer's ``on_wait`` callback and never folded into
      ``elapsed_seconds``; on a slot-busy result it is the elapsed bounded
      wait.
    * ``exit_code`` — the process exit code (``None`` when no child ran);
      negative means signal-terminated.
    * ``signal`` — the terminating signal number when ``exit_code < 0``, else
      ``None``.

    Non-zero results without parsable failures carry ``killed`` (signal-
    terminated) or ``unparsed_failure`` (non-zero exit, no parsable failure)
    as ``notice_code``; structured failures always win over those codes.

    Verdict provenance (SA-0MVQHVJEX0071R7G): ``cached`` states whether every
    command of the suite was served from the cache, and ``cache_entries``
    carries one metadata mapping per cache-served command (``command``,
    ``completed_at``, ``scope``, ``fingerprint_kind``) so a consumer can
    attribute — and audit — a verdict without re-reading the cache.
    """
    cwd = cwd or REPO_ROOT
    resolvable_scope = "full"
    changed_files: set[str] = set()
    # Group-based command resolution (SA-0MUJKF2HP001BR51): when a group is
    # specified, use its commands directly and record the group in test_type.
    if group is not None:
        group_cmds = group_commands(group, cwd)
        if group_cmds is None:
            raise ValueError(f"Unknown group '{group}' — define it in .pi/test-config.json.")
        commands = group_cmds
        # Record the group name as the test_type for distinct cache keys.
        test_type = f"group:{group}"
        resolvable_scope = "full"
    if commands is None:
        if scope == "changed" and test_type == DEFAULT_TEST_TYPE:
            # Changed-file selection drives every suite name; the selection is
            # narrowed to the named suite below. Any failure to produce a
            # subset (no changed/selectable files, no matching group) falls
            # back to full. Declared ``suiteCommands`` never disable
            # selection — they define the full suite only
            # (SA-0MVL7YOMJ006UMDO).
            changed_files = compute_changed_files(cwd, base_ref=base_ref)
            changed_commands = changed_scope_commands(
                cwd, base_ref=base_ref, changed_files=changed_files
            )
            if changed_commands is not None:
                cmds = changed_commands
                if name == "pytest":
                    cmds = [c for c in cmds if "pytest" in c]
                elif name == "node":
                    cmds = [c for c in cmds if "node" in c]
                if cmds:
                    commands = cmds
                    resolvable_scope = "changed"
        if commands is None:
            # Full scope (including changed-scope fallback — report "full" so
            # consumers never treat a partial selection as complete).
            if test_type == DEFAULT_TEST_TYPE:
                if scope == "changed":
                    # Changed-scope fallback: resolve through
                    # ``full_suite_commands()`` — the single source of truth
                    # (F2 AC4) — so a repo's pinned suiteCommands (custom
                    # interpreter) are honoured for every suite name (the
                    # fallback never silently swaps to the legacy bare
                    # ``pytest`` token and a different interpreter,
                    # OSL-0MVNWYRQB0095WXQ). A pinned/conventional suite
                    # that defines no matching command raises the
                    # unknown-suite-style error instead of silently running
                    # nothing. Tool-less repos (empty full set) keep the
                    # legacy resolution so full-scope behaviour is unchanged.
                    full = full_suite_commands(cwd)
                    if full:
                        commands = filter_commands_for_suite(name, full)
                        if not commands:
                            raise ValueError(
                                f"unknown suite: {name} — the repo's suite "
                                "commands define no matching command"
                            )
                if commands is None:
                    # Legacy name-based resolution (AC5; unchanged for direct
                    # full-scope runs).
                    if name == "pytest":
                        commands = [pytest_command()]
                    elif name == "node":
                        commands = node_suite_commands(cwd)
                    elif name == "all":
                        commands = full_suite_commands(cwd)
                    else:
                        raise ValueError(f"unknown suite: {name}")
            else:
                type_cmds = resolve_type_commands(cwd, test_type)
                commands = filter_commands_for_suite(name, type_cmds)
                if not commands:
                    raise TypeResolutionError(
                        f"Test type '{test_type}' defines no commands for the "
                        f"'{name}' suite (resolved: {', '.join(type_cmds)})."
                    )
            resolvable_scope = "full"
    else:
        # Explicit command override (a resolved type profile, or a
        # changed-scope selection by implement.py): the caller has already
        # selected the command set, so the passed scope is authoritative.
        # Recording a changed-scope selection as "full" would mislabel a
        # partial run as full-suite evidence (SA-0MUH1MJL6003767Q).
        resolvable_scope = scope if scope in ("full", "changed") else "full"
    command = " && ".join(commands)
    # Lane selection (SA-0MVWZQ269009SJXV AC1): a partial run gets its own
    # "test-fast" pool so it never queues behind a full suite. Derived from the
    # RESOLVED scope, so a changed-scope run that fell back to the full suite
    # correctly reports (and contends in) the full lane.
    slot_lane = slot_lane_for(resolvable_scope, test_type)

    all_failures: list[dict[str, str]] = []
    #: Failures harvested from stages the retry runner accepted
    #: (``status=0`` + ``retry-clean``/``accepted-transient``): reported as
    #: diagnostics (``discarded_failures`` + a stable notice), never as suite
    #: failures, so a recovered contention flake does not turn a green tree red
    #: (CG-0MWEHG69O005VM17).
    all_discarded: list[dict[str, str]] = []
    overall_returncode = 0
    cached_flags: list[bool] = []
    cache_entries: list[dict[str, Any]] = []
    interpreters: set[str] = set()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    #: Command-execution wall times (queue wait excluded) and queue waits, both
    #: measured per executed command so a partially cached suite still reports
    #: exactly what it measured (SA-0MVU8XRBQ000DG9D).
    elapsed_samples: list[float] = []
    slot_waits: list[float] = []

    def _result(**overrides: Any) -> dict[str, Any]:
        """Suite result carrying the shared facts and diagnostics.

        Every return path — green, structured failure, timeout, kill,
        unparsed exit, slot busy, command not found — reports the same fact
        fields, so no consumer has to infer what happened from prose
        (SA-0MVU8XRBQ000DG9D).
        """
        result: dict[str, Any] = {
            "success": False,
            "returncode": None,
            "command": command,
            "failures": [],
            # Retry-recovered failures (CG-0MWEHG69O005VM17): records from
            # stages the retry runner accepted (status=0 +
            # retry-clean/accepted-transient), reported as diagnostics — never
            # in ``failures`` and never affecting ``success``. Additive field;
            # consumers that predate it simply never see it populated.
            "discarded_failures": [],
            "notice": "",
            "notice_code": "",
            "stdout": _output_tail("\n".join(stdout_parts)),
            "stderr": _output_tail("\n".join(stderr_parts)),
            "scope": resolvable_scope,
            "type": test_type,
            "interpreter": (
                "mixed" if len(interpreters) > 1 else next(iter(interpreters), "")
            ),
            "changed_files": (
                sorted(changed_files) if resolvable_scope == "changed" else []
            ),
            "configured_timeout": timeout,
            "elapsed_seconds": _measured_seconds(elapsed_samples),
            "slot_wait_seconds": sum(slot_waits),
            "slot_lane": slot_lane,
            "exit_code": None,
            "signal": None,
            # Verdict provenance (SA-0MVQHVJEX0071R7G): every return path
            # states whether the verdict was cache-served and, if so, which
            # entries were reused.
            "cached": use_cache and all(cached_flags) if cached_flags else False,
            "cache_entries": cache_entries,
            # Mid-run checkout movement (SA-0MW3FF18I003KGZE): None on every
            # stable path; the flag dict {notice_code, checkout, before_commit,
            # after_commit, message} when the guarded checkout moved while a
            # command executed.
            "state_changed_during_run": None,
        }
        result.update(overrides)
        return result

    for cmd in commands:
        command_waits: list[float] = []
        command_elapsed: list[float] = []
        # Set when run_cached reports the guarded checkout moved while this
        # command executed (SA-0MW3FF18I003KGZE).
        state_changed_flag: dict[str, Any] | None = None

        def _record_wait(
            _queued_at: float,
            wait_seconds: float,
            _sink: list[float] = command_waits,
        ) -> None:
            # The pacer reports the queue wait exactly once per execution, so
            # this is the single measurement — never a second timer, and never
            # folded into the command's own elapsed time (SA-0MVU8XRBQ000DG9D).
            _sink.append(wait_seconds)

        paced = paced_runner(
            runner=_timed_suite_runner(command_elapsed),
            on_wait=_record_wait,
            lane=slot_lane,
        )
        queued_at = time.monotonic()
        try:
            if use_cache:
                run = run_cached(
                    cmd,
                    cwd=str(cwd),
                    force=force,
                    no_cache=no_cache,
                    ttl=CACHE_TTL_SECONDS,
                    timeout=timeout,
                    runner=paced,
                    scope=resolvable_scope,
                    test_type=test_type,
                    fingerprint=fingerprint,
                )
                proc = SimpleNamespace(
                    stdout=run["stdout"], stderr=run["stderr"], returncode=run["exit_code"]
                )
                cached_flags.append(run["cached"])
                state_changed_flag = run.get("state_changed_during_run")
                if run["cached"]:
                    cache_entries.append(_cache_entry_metadata(run, cmd))
                if run.get("interpreter"):
                    interpreters.add(run["interpreter"])
            else:
                proc = paced(cmd, str(cwd), timeout)
                cached_flags.append(False)
                if is_pytest_command(cmd):
                    interpreters.add(resolve_pytest_executable())
        except FileNotFoundError as exc:
            slot_waits.extend(command_waits)
            elapsed_samples.extend(command_elapsed)
            return _result(
                command=command,
                notice=f"command not found: {exc.filename}",
                notice_code=NOTICE_COMMAND_NOT_FOUND,
            )
        except subprocess.TimeoutExpired:
            slot_waits.extend(command_waits)
            elapsed_samples.extend(command_elapsed)
            return _result(
                notice=f"suite timed out after {timeout}s: {name}",
                notice_code=NOTICE_SUITE_TIMEOUT,
            )
        except TestConcurrencyTimeout as exc:
            # Concurrency ceiling stayed saturated past the bounded wait:
            # report a clear failure notice (never a crash, never fail-fast
            # before the bound) — SA-0MTG5U75A001F1RG AC1. Nothing executed,
            # so the whole bounded window was queue wait (SA-0MVU8XRBQ000DG9D).
            slot_waits.append(time.monotonic() - queued_at)
            return _result(notice=str(exc), notice_code=NOTICE_SLOT_BUSY)
        slot_waits.extend(command_waits)
        elapsed_samples.extend(command_elapsed)

        output = f"{proc.stdout}\n{proc.stderr}"
        stdout_parts.append(proc.stdout or "")
        stderr_parts.append(proc.stderr or "")
        # A checkout that moved mid-run (SA-0MW3FF18I003KGZE) makes this
        # command's output a mixed-tree verdict: its content is evidence for
        # no single commit. No failure is parsed from it (the parser would
        # happily match failures that exist in neither tree), the suite is
        # forced non-green with the distinct STATE_CHANGED_EXIT_CODE, and the
        # detail is carried on the result for consumers.
        if state_changed_flag:
            overall_returncode = STATE_CHANGED_EXIT_CODE
            # One moved command invalidates the whole suite: the remaining
            # commands would run against the same unstable checkout and
            # contribute nothing usable.
            break
        failures, discarded = parse_failures_with_retry_verdict(cmd, output)
        all_failures.extend(failures)
        all_discarded.extend(discarded)
        if proc.returncode != 0:
            overall_returncode = proc.returncode

    state_changed_detail = (
        state_changed_flag if state_changed_flag is not None else None
    )
    flagged_notice = ""
    flagged_notice_code = ""
    if state_changed_detail is not None:
        flagged_notice_code = NOTICE_STATE_CHANGED_DURING_RUN
        flagged_notice = (
            f"[{NOTICE_STATE_CHANGED_DURING_RUN}] "
            f"checkout {state_changed_detail.get('checkout')} changed while "
            f"the test suite executed (HEAD "
            f"{state_changed_detail.get('before_commit') or '<none>'} -> "
            f"{state_changed_detail.get('after_commit') or '<none>'}): the "
            f"mixed tree exists at no single commit, so the verdict is "
            f"discarded — re-run the suite from a stable checkout."
        )

    # Retry-recovered discard notice (CG-0MWEHG69O005VM17): informational,
    # never a failure. A stage the retry runner accepted stays green; the
    # notice names the flaked file(s) so an operator can still see what the
    # retry recovered. Carried on ``discarded_failures`` (structured) and as a
    # stable notice combination for per-suite printing.
    discarded_notice = ""
    discarded_notice_code = ""
    if all_discarded:
        discarded_notice_code = NOTICE_RETRY_DISCARDED
        flaked = ", ".join(
            sorted({f.get("test_name", "<unknown>") for f in all_discarded})
        )
        discarded_notice = (
            f"[{NOTICE_RETRY_DISCARDED}] {len(all_discarded)} failure(s) on "
            f"superseded attempt(s) were accepted as transient by the retry "
            f"runner and discarded (not suite failures): {flaked}"
        )

    result = _result(
        success=overall_returncode == 0 and not all_failures,
        returncode=overall_returncode,
        exit_code=overall_returncode,
        command=command,
        failures=all_failures,
        discarded_failures=all_discarded,
        notice=flagged_notice or discarded_notice,
        notice_code=flagged_notice_code or discarded_notice_code,
        cached=use_cache and all(cached_flags) if cached_flags else False,
        state_changed_during_run=state_changed_detail,
    )
    # Classification precedence (SA-0MVU8XRBQ000DG9D): a parsable failure is
    # always a code failure. Only a non-zero suite with NO parsable failure is
    # classified as an infrastructure condition, and a signal-terminated child
    # (negative return code — OOM kill, worker crash) is distinguished from a
    # plain non-zero exit. Cache-served non-zero results are classified from
    # their stored exit code and output too (their elapsed time is ``None``).
    # A flagged (moved-checkout) run is never given a failure-style
    # classification: its notice code already names the movement.
    if (
        not all_failures
        and overall_returncode != 0
        and state_changed_detail is None
    ):
        if overall_returncode < 0:
            signal_number = abs(overall_returncode)
            result["signal"] = signal_number
            result["notice_code"] = NOTICE_KILLED
            result["notice"] = f"suite killed by signal {signal_number}: {name}"
        else:
            result["notice_code"] = NOTICE_UNPARSED_FAILURE
            result["notice"] = (
                f"suite exited {overall_returncode} with no parsable "
                f"failures: {name}"
            )
    return result


def _render_seconds(value: float | None) -> str:
    """Render a measured duration for humans, naming an unmeasured one."""
    return "n/a (not measured)" if value is None else f"{value:.2f}s"


def _render_suite_facts(result: dict[str, Any]) -> str:
    """One-line human rendering of a suite result's diagnostic facts.

    Mirrors the machine-readable fields (SA-0MVU8XRBQ000DG9D) so an operator
    reading the CLI sees the same configured timeout, elapsed time, queue
    wait, exit code/signal and command the JSON report carries.
    """
    parts = [f"elapsed={_render_seconds(result.get('elapsed_seconds'))}"]
    slot_wait = result.get("slot_wait_seconds")
    if slot_wait:
        parts.append(f"slot_wait={slot_wait:.2f}s")
    if result.get("slot_lane"):
        parts.append(f"lane={result['slot_lane']}")
    exit_code = result.get("exit_code")
    if exit_code is not None:
        parts.append(f"exit_code={exit_code}")
    signal_number = result.get("signal")
    if signal_number is not None:
        parts.append(f"signal={signal_number}")
    if result.get("configured_timeout") is not None:
        parts.append(f"configured_timeout={result['configured_timeout']}s")
    if result.get("command"):
        parts.append(f"command={result['command']}")
    return ", ".join(parts)


def run_all(
    suites: tuple[str, ...] = ("all",),
    cwd: Path | None = None,
    timeout: int = 600,
    use_cache: bool = True,
    force: bool = False,
    no_cache: bool = False,
    scope: str = "full",
    base_ref: str = "origin/dev",
    commands: list[str] | None = None,
    test_type: str = DEFAULT_TEST_TYPE,
    group: str | None = None,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Run the selected suites and aggregate failures.

    The default ``("all",)`` resolves the repo's full suite through
    ``full_suite_commands(cwd)`` — the single source of truth (F2 AC4): a
    no-pytest repo never runs a phantom pytest command, and a vitest/npm repo
    runs its real ``npm --silent test``. Explicit ``("pytest",)``/``("node",)``
    selections keep the historical per-suite behavior.

    *scope*: ``"changed"`` resolves changed-file-selected tests for the
    ``"all"`` suite (falling back to full when a subset is impossible); the
    result carries ``scope`` so consumers can distinguish partial from full.

    *commands* / *test_type*: an explicit typed command profile (resolved by
    :func:`resolve_type_commands`) and the type name it belongs to. The type
    is recorded in each suite result and in the aggregate.

    *group*: when set, runs the named test group instead of suite-based
    resolution (SA-0MUJKF2HP001BR51).

    In addition to the per-suite detail (kept intact under ``suites``) the
    aggregate carries ``notice_codes``, ``elapsed_seconds_total`` (sum of
    measured command executions, ``None`` when nothing ran) and
    ``slot_wait_seconds`` (sum of queue waits) — the two durations are
    aggregated separately so neither can be mistaken for the other
    (SA-0MVU8XRBQ000DG9D).
    Verdict provenance (SA-0MVQHVJEX0071R7G): the aggregate carries ``cached``
    (True only when every selected suite was served from the cache) and
    ``cache_entries`` (the metadata of every served entry, one per cached
    command) so consumers can attribute the aggregate verdict.
    """
    results: dict[str, Any] = {}
    all_failures: list[dict[str, str]] = []
    # Retry-recovered failures aggregated alongside ``failures``
    # (CG-0MWEHG69O005VM17): each record gains the suite attribution, exactly
    # as ``failures`` does, so a consumer sees which suite discarded what.
    all_discarded: list[dict[str, str]] = []

    # Full-scope runs are the churn-heavy path behind the /tmp-inode defect
    # (SA-0MW2QED9M005FWOK): prune stale pytest roots once before executing so
    # thousands of tiny pytest-of-<user> roots cannot exhaust the inode budget
    # inside a suite subprocess. Gated on the REQUESTED scope AND the
    # commands-is-None (canonical full-resolution) branch so an explicit
    # command override (typed profiles, finish-gate changed scope, the
    # guarded-run E2E fixtures) is untouched (SA-0MW3ENF32006BGN1 AC2;
    # mirror of the ``if commands is None`` full-resolution branch).
    # Fail-open (AC3): any prune error is logged and swallowed; the run's
    # exit status, cache entry and scope resolution are unchanged.
    if scope == "full" and commands is None:
        _prune_stale_roots_before_run()
    notices: list[str] = []
    notice_codes: list[str] = []
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    resolved_scopes: list[str] = []
    # Diagnostic aggregates (SA-0MVU8XRBQ000DG9D): execution time and queue
    # wait are summed SEPARATELY, so a queued run can never look like a slow
    # suite (or vice versa). Per-suite detail stays in ``suites``.
    elapsed_total = 0.0
    elapsed_measured = False
    slot_wait_total = 0.0
    # Non-fatal resolution notices (SA-0MVQQ9NV4005UKYZ AC3/AC4b): reported for
    # visibility only — they never change the resolved command set or the exit
    # status. Computed once, from the suite's project root.
    resolution_notices = full_suite_notices(Path(cwd or REPO_ROOT).resolve())
    for notice in resolution_notices:
        print(f"run_tests: notice [{notice['code']}] {notice['message']}", file=sys.stderr)
    #: Moved-checkout flags (SA-0MW3FF18I003KGZE): carried through the
    #: aggregate so a consumer never loses the detail a suite-level notice
    #: references by code alone.
    state_changed_flags: list[dict[str, Any]] = []
    with Timer("run_all") as _all_timer:
        for name in suites:
            with Timer(f"suite:{name}"):
                result = run_suite(
                    name,
                    cwd=cwd,
                    timeout=timeout,
                    use_cache=use_cache,
                    force=force,
                    no_cache=no_cache,
                    commands=commands,
                    scope=scope,
                    base_ref=base_ref,
                    test_type=test_type,
                    group=group,
                    fingerprint=fingerprint,
                )
            results[name] = result
            suite_flag = result.get("state_changed_during_run")
            if suite_flag:
                state_changed_flags.append(suite_flag)
            resolved_scopes.append(result["scope"])
            suite_elapsed = result.get("elapsed_seconds")
            if suite_elapsed is not None:
                elapsed_total += suite_elapsed
                elapsed_measured = True
            slot_wait_total += result.get("slot_wait_seconds") or 0.0
            for failure in result["failures"]:
                all_failures.append({**failure, "suite": name})
            for discarded in result.get("discarded_failures") or []:
                all_discarded.append({**discarded, "suite": name})
            if result.get("notice"):
                notices.append(result["notice"])
            if result.get("notice_code"):
                notice_codes.append(result["notice_code"])
            if result.get("stdout"):
                stdout_parts.append(result["stdout"])
            if result.get("stderr"):
                stderr_parts.append(result["stderr"])
    return {
        "success": all(r["success"] for r in results.values()),
        "suites": results,
        "failures": all_failures,
        # Retry-recovered diagnostics (CG-0MWEHG69O005VM17): additive, never
        # counted in ``success``/``failures`` — a stage the retry runner
        # accepted stays green.
        "discarded_failures": all_discarded,
        "notices": notices,
        "notice_codes": notice_codes,
        # Moved-checkout flags (SA-0MW3FF18I003KGZE): the detail behind each
        # ``state_changed_during_run`` notice code.
        "state_changed_during_run": state_changed_flags,
        # Resolution notices are informational and carry a stable machine-
        # readable shape (code/directories/source); they are deliberately kept
        # out of ``notices``/``notice_codes`` so a consumer classifying a
        # FAILED run is never confused by a non-fatal notice.
        "resolution_notices": resolution_notices,
        "stdout": "\n".join(stdout_parts),
        "stderr": "\n".join(stderr_parts),
        "scope": scope,
        "type": test_type,
        "resolved_scopes": resolved_scopes,
        "elapsed_seconds_total": elapsed_total if elapsed_measured else None,
        "slot_wait_seconds": slot_wait_total,
        # Lane provenance (SA-0MVWZQ269009SJXV AC4): which semaphore namespace(s)
        # the selected suites contended in, so an operator can tell a slow run
        # from a queue-delayed one without reading stderr.
        "slot_lanes": sorted(
            {
                result["slot_lane"]
                for result in results.values()
                if result.get("slot_lane")
            }
        ),
        # Verdict provenance (SA-0MVQHVJEX0071R7G): the aggregate is
        # cache-served only when every suite was, and the entries reported are
        # the union of the suites' served entries (one per cached command).
        "cached": bool(results) and all(r.get("cached") for r in results.values()),
        "cache_entries": [
            entry
            for result in results.values()
            for entry in result.get("cache_entries") or []
        ],
        "timing": _all_timer.to_dict(),
    }


def rerun_failures(
    failures: list[dict[str, str]],
    cwd: Path | None = None,
    timeout: int = 600,
) -> list[dict[str, str]]:
    """Re-run individually failing tests once to verify flakiness.

    Returns the failures that still fail on re-run (stable failures). Tests
    that pass on re-run are dropped with a note appended to their record.
    """
    stable: list[dict[str, str]] = []
    for failure in failures:
        test_name = failure.get("test_name", "")
        suite = failure.get("suite", "pytest")
        if suite == "pytest" and test_name:
            cmd = canonicalize_quiet_test_command(f"pytest {test_name}")
        else:
            # Node tests are re-run per file — keep as stable without a rerun.
            stable.append(failure)
            continue
        try:
            # A per-test rerun is a partial run: it belongs to the fast lane so
            # it never queues behind the full suite that just failed
            # (SA-0MVWZQ269009SJXV AC1).
            with _slot_lane(TEST_SLOT_LANE_FAST), _test_concurrency_slot():
                proc = _run_cmd(
                    shlex.split(executable_test_command(cmd)),
                    cwd=cwd or REPO_ROOT,
                    timeout=timeout,
                )
        except FileNotFoundError:
            stable.append(failure)
            continue
        except TestConcurrencyTimeout as exc:
            # Saturation prevented the rerun: keep as stable with a note.
            failure["note"] = f"rerun skipped (concurrency saturated): {exc}"
            stable.append(failure)
            continue
        if proc.returncode == 0:
            failure["flaky"] = True
            failure["note"] = "passed on re-run; treated as flaky"
            continue
        stable.append(failure)
    return stable


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


#: Stable prefixes of the gate-subject provenance lines ``run_tests.py``
#: prints on **stderr** when ``--gate-commit`` is used (SA-0MVTRBT50005DUEH).
#: The pre-push hook echoes the ``gate-*`` lines to the pusher and parses
#: :data:`GATE_ROOT_PREFIX` to mirror the cache of the checkout the gate
#: actually resolved — so the format is a contract with `.githooks/pre-push`.
GATE_SUBJECT_PREFIX = "run_tests: gate-subject "
GATE_ROOT_PREFIX = "run_tests: gate-root "
GATE_VERDICT_PREFIX = "run_tests: gate-verdict "
GATE_REFUSED_PREFIX = "run_tests: gate-refused "


class GateSubjectError(RuntimeError):
    """The pushed commit cannot be bound to a clean checkout it can be gated in.

    Raised by :func:`resolve_gate_subject` when ``--gate-commit`` names a commit
    that is not present locally, that no checkout holds, or whose only
    candidate checkouts are dirty. The caller refuses the push (exit code 2)
    instead of measuring a different tree.
    """


def _git_output(cwd: Path, *args: str) -> str | None:
    """Run a read-only git command in *cwd*; ``None`` when it cannot answer.

    Uses :func:`shared.git_sandbox.git_env_for_repo` so a leaked ``GIT_DIR`` /
    ``GIT_WORK_TREE`` cannot make the subject resolution describe a different
    repository (the same scrub the fingerprint uses).
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=git_env_for_repo(str(cwd)),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def parse_worktree_listing(output: str) -> list[tuple[Path, str]]:
    """Return ``(path, HEAD)`` for each checkout in ``git worktree list --porcelain``.

    A bare entry (``bare`` line, no ``HEAD``) is skipped: it has no working
    tree to run a suite in. Order is git's own (the main checkout first), so a
    deterministic candidate is picked without an arbitrary sort.
    """
    entries: list[tuple[Path, str]] = []
    path: str | None = None
    sha: str | None = None
    for line in output.splitlines():
        if line.startswith("worktree "):
            if path and sha:
                entries.append((Path(path), sha))
            path, sha = line[len("worktree "):], None
        elif line.startswith("HEAD "):
            sha = line[len("HEAD "):].strip()
    if path and sha:
        entries.append((Path(path), sha))
    return entries


def resolve_gate_subject(
    gate_commit: str, project_root: Path
) -> tuple[Path, dict[str, Any]]:
    """Resolve the pushed commit to a clean checkout the gate can measure.

    The gate's verdict is only evidence about the pushed commit when the tree
    it fingerprints *is* that commit with no dirty paths folded in
    (SA-0MVTRBT50005DUEH). Candidates, in order:

    1. *project_root* itself, when its ``HEAD`` is the pushed commit and it has
       no dirty paths (the normal ``implement.py finish`` push from the
       worktree that holds the commit — a cheap in-place run);
    2. a checkout listed by ``git worktree list`` whose ``HEAD`` is the pushed
       commit and which is clean (a manual push from a dirty main checkout
       still gates the commit, in the pusher's own clean worktree);
    3. otherwise refuse: :class:`GateSubjectError` naming the commit, every
       candidate tried (and why it was rejected) and the escape hatch.

    Materialising a throwaway detached worktree is deliberately *not* done: a
    fresh checkout has no installed dependencies, so suite commands could fail
    for reasons unrelated to the pushed tree.

    The pushed commit's **tree** SHA (``git rev-parse <commit>^{tree}``) is
    derived as part of the evidence (SA-0MW7C6BMZ00931JG). It is the explicit
    fingerprint the gate binds its verdict to, so the entry lands in the
    ``fingerprint_kind="tree"`` namespace shared with the finish gate and is
    reusable by any tree-keyed consumer. A tree that cannot be computed is a
    refusal, never a fallback to the loose worktree key.

    Returns:
        ``(root, evidence)`` where *root* is the checkout to run/cache against
        and *evidence* carries ``pushed`` (full sha), ``tree`` (the pushed
        commit's tree sha), ``root`` and ``checkout`` (``"in-place"`` or
        ``"worktree"``).

    Raises:
        GateSubjectError: when the commit is not present locally, its tree
            cannot be computed, or no clean checkout holds it.
    """
    root = Path(project_root).resolve()
    commit = (gate_commit or "").strip()
    escape = (
        "Set TEST_SCOPE_SKIP=1 to bypass the full-suite gate "
        "(not recommended)."
    )
    if not re.fullmatch(r"[0-9a-fA-F]{4,40}", commit):
        raise GateSubjectError(
            f"--gate-commit expects a git commit sha, got {gate_commit!r}. {escape}"
        )

    full = (
        _git_output(root, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
        or ""
    ).strip()
    if not full:
        raise GateSubjectError(
            f"pushed commit {commit[:12]} is not present in this repository, so it "
            f"cannot be gated locally (fetch it and retry from a checkout that "
            f"holds it). {escape}"
        )

    # The pushed commit's tree is the fingerprint the verdict is bound to
    # (SA-0MW7C6BMZ00931JG). It must be computable: an unavailable tree means
    # the gate cannot prove which tree it measured, so it refuses rather than
    # falling back to a loose worktree key (the cache's provenance contract).
    tree = (_git_output(root, "rev-parse", f"{full}^{{tree}}") or "").strip()
    if not tree:
        raise GateSubjectError(
            f"pushed commit {full[:12]} is present in {root} but its tree SHA "
            f"cannot be computed (`git rev-parse {full[:12]}^{{tree}}` failed), so "
            "the gate cannot bind its verdict to the pushed tree. "
            f"{escape}"
        )

    rejected: list[str] = []
    head = (_git_output(root, "rev-parse", "HEAD") or "").strip()
    if head == full:
        if is_worktree_clean(root):
            return root, {
                "pushed": full,
                "tree": tree,
                "root": str(root),
                "checkout": "in-place",
            }
        rejected.append(f"{root} (HEAD matches, but the checkout has uncommitted changes)")
    else:
        rejected.append(f"{root} (HEAD is {head[:12] or 'unknown'})")

    listing = _git_output(root, "worktree", "list", "--porcelain") or ""
    for candidate, candidate_head in parse_worktree_listing(listing):
        if candidate_head != full or candidate.resolve() == root:
            continue
        if not candidate.is_dir():
            rejected.append(f"{candidate} (HEAD matches, but the checkout is missing)")
            continue
        if is_worktree_clean(candidate):
            return candidate.resolve(), {
                "pushed": full,
                "tree": tree,
                "root": str(candidate.resolve()),
                "checkout": "worktree",
            }
        rejected.append(
            f"{candidate} (HEAD matches, but the checkout has uncommitted changes)"
        )

    tried = "; ".join(rejected) if rejected else "no checkout of this repository holds it"
    raise GateSubjectError(
        f"no clean checkout holds pushed commit {full[:12]}: {tried}. The full-suite "
        "gate only measures a checkout of the pushed commit, so it refuses instead "
        f"of reporting a verdict about a different tree. {escape}"
    )


def _emit_gate_subject(evidence: dict[str, Any]) -> None:
    """Print the state about to be validated (AC3 provenance), one fact per line.

    The root goes on its own line so a path containing spaces survives the
    hook's parse (`.githooks/pre-push` takes the rest of the line verbatim).

    The gate is tree-keyed (SA-0MW7C6BMZ00931JG): it reports the pushed
    commit's ``tree`` and ``fingerprint_kind=tree`` so the pusher can see the
    verdict belongs to the pushed tree, not the loose checkout it resolved.
    """
    print(
        f"{GATE_SUBJECT_PREFIX}pushed={evidence['pushed']} "
        f"checkout={evidence['checkout']} fingerprint_kind=tree "
        f"tree={evidence['tree']}",
        file=sys.stderr,
    )
    print(f"{GATE_ROOT_PREFIX}{evidence['root']}", file=sys.stderr)


def _gate_verdict_facts(
    evidence: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    """The gate's verdict provenance as structured facts.

    Shared by the ``run_tests: gate-verdict`` stderr line and the gate's JSON
    ``gate`` object, so both report the same ``cache_hit``,
    ``cache_age_seconds``, ``fingerprint_kind`` and ``tree``
    (SA-0MW7C6BMZ00931JG AC3). ``cache_age_seconds`` is ``None`` on a miss
    (nothing was reused) and the age of the **oldest** reused entry on a hit —
    the weakest evidence in the aggregate.
    """
    entries = result.get("cache_entries") or []
    kinds = sorted({entry.get("fingerprint_kind") or "" for entry in entries} - {""})
    cache_hit = bool(result.get("cached"))
    ages = [
        float(entry["cache_age_seconds"])
        for entry in entries
        if isinstance(entry.get("cache_age_seconds"), (int, float))
    ]
    return {
        "verdict": "cached" if cache_hit else "executed",
        "cache_hit": cache_hit,
        "cache_age_seconds": max(ages) if ages else None,
        "fingerprint_kind": ",".join(kinds) or "tree",
        "entries": len(entries),
        "pushed": evidence["pushed"],
        "tree": evidence["tree"],
    }


def _emit_gate_verdict(evidence: dict[str, Any], result: dict[str, Any]) -> None:
    """Print executed-vs-cached plus the cache state the verdict belongs to.

    Reports, in the ``run_tests: gate-verdict`` provenance line the hook
    echoes, whether the verdict was served from the cache (``cache_hit``), the
    age in seconds of the reused entry (``cache_age_seconds`` — ``n/a`` on a
    miss) and that the verdict is bound to the pushed tree
    (``fingerprint_kind=tree``, ``tree=<sha>``). This makes a reused verdict
    visible and attributable to the pusher (SA-0MW7C6BMZ00931JG AC3).
    """
    facts = _gate_verdict_facts(evidence, result)
    age = (
        "n/a"
        if facts["cache_age_seconds"] is None
        else f"{facts['cache_age_seconds']:.1f}"
    )
    print(
        f"{GATE_VERDICT_PREFIX}verdict={facts['verdict']} "
        f"sha={evidence['pushed']} tree={evidence['tree']} "
        f"fingerprint_kind={facts['fingerprint_kind']} "
        f"cache_hit={'true' if facts['cache_hit'] else 'false'} "
        f"cache_age_seconds={age} entries={facts['entries']}",
        file=sys.stderr,
    )


#: A full hexadecimal object name as ``git write-tree`` emits: 40 characters
#: for SHA-1 repositories, 64 for SHA-256. Anything else cannot name a tree,
#: so an explicit ``--fingerprint`` value is rejected rather than silently
#: keying the cache on an arbitrary string (SA-0MW7C6AYO0062GJB AC2).
TREE_FINGERPRINT_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")


class FingerprintRejected(ValueError):
    """An explicit ``--fingerprint`` value cannot bind a tree-keyed run.

    Raised by :func:`resolve_explicit_fingerprint` for a value that is not a
    usable tree SHA in the project root; the CLI turns it into exit 2 with
    the actionable message. Fail-closed by design (SA-0MW7C6AYO0062GJB
    AC2): a rejected value stops the run instead of being dropped, because a
    silent drop would fall back to a loose worktree key and hand a
    tree-requesting consumer a verdict recorded for a different state.
    """


def resolve_explicit_fingerprint(value: str, project_root: Path) -> str:
    """Validate and normalise an explicit ``--fingerprint`` tree SHA.

    Returns the value lower-cased. Raises :class:`FingerprintRejected` with an
    actionable message when *value* cannot name a tree in *project_root*:

    * not a full 40-character (SHA-1) or 64-character (SHA-256) hexadecimal
      object name — the shape ``git write-tree`` emits;
    * an object the repository does not hold;
    * an object that is a commit, blob or tag rather than a tree.

    Fail-closed (AC2): a rejected value stops the run (exit 2 at the CLI)
    instead of degrading to the loose :func:`compute_git_state` key, which
    would record a tree-requesting caller's verdict against the wrong state.
    Repository-override variables are scrubbed like every other git call in
    the runner (SA-0MUIA3OE40001QJX) so a leaked ``GIT_DIR`` cannot have the
    object resolved in a different repository.
    """
    candidate = value.strip()
    if not TREE_FINGERPRINT_RE.fullmatch(candidate):
        raise FingerprintRejected(
            f"value {value!r} is not a usable tree SHA: a full 40-character "
            "(SHA-1) or 64-character (SHA-256) hexadecimal object name is "
            "required — the value `git write-tree` reports for the staged "
            "change set. Supply that tree SHA, or omit --fingerprint to use "
            "the loose worktree key."
        )
    env = subprocess_env_with_scrubbed_overrides()
    try:
        proc = subprocess.run(
            ["git", "cat-file", "-t", candidate],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FingerprintRejected(
            f"cannot verify tree SHA {candidate} in {project_root}: {exc}. "
            "Supply a tree SHA from this repository, or omit --fingerprint "
            "to use the loose worktree key."
        ) from exc
    obj_type = proc.stdout.strip()
    if proc.returncode != 0 or not obj_type:
        raise FingerprintRejected(
            f"{project_root} does not hold an object named {candidate} "
            "(`git cat-file -t` failed). Supply a tree SHA from this "
            "repository, or omit --fingerprint to use the loose worktree key."
        )
    if obj_type != "tree":
        raise FingerprintRejected(
            f"{candidate} names a {obj_type}, not a tree — the explicit "
            "fingerprint must be the tree a commit would produce "
            "(`git write-tree`), not a commit/blob/tag SHA. Supply that tree "
            "SHA, or omit --fingerprint to use the loose worktree key."
        )
    return candidate.lower()


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser.

    ``--fingerprint`` is validated in :func:`main` by
    :func:`resolve_explicit_fingerprint` and rejected (exit 2) when it cannot
    bind a ``--scope full`` run to an explicit would-be-commit tree SHA —
    fail-closed, never a silent fallback to a loose worktree key
    (SA-0MW7C6AYO0062GJB).
    """
    parser = argparse.ArgumentParser(
        description="Run the full project test suite in quiet mode and report failures."
    )
    parser.add_argument(
        "--suite",
        choices=("pytest", "node", "all"),
        default="all",
        help="Which suite(s) to run (default: all).",
    )
    parser.add_argument(
        "--type",
        dest="test_type",
        default=DEFAULT_TEST_TYPE,
        metavar="TYPE",
        help="Test type/profile to run: 'full' (default), 'unit', 'smoke', or "
        "any type defined by the project's local test extension "
        "(.pi/skills_extensions/test/extension.json). The type selects the "
        "command profile; --scope selects full-vs-changed within it. Only "
        "'full' populates the audit-accepted full-suite cache entry.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output.",
    )
    parser.add_argument(
        "--parent-work-item-id",
        default=None,
        help="Parent work item id passed through to triage check_or_create.py.",
    )
    parser.add_argument(
        "--rerun-failures",
        action="store_true",
        help="Re-run failing tests once to verify flakiness before triage.",
    )
    parser.add_argument("--timeout", type=int, default=None, help="Per-suite timeout in seconds.")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the run cache entirely (execute fresh, do not store).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force a fresh run, refreshing the cache entry.",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print summary lines (e.g. 'Test Files', 'Tests', 'failed') from a cached run without executing.",
    )
    parser.add_argument(
        "--summary-grep",
        default=None,
        help="Custom grep pattern for --summary line filtering.",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help="Explicit project root to test/cache against (default: detected from "
        "cwd via git rev-parse --show-toplevel, falling back to the framework "
        "install location).",
    )

    parser.add_argument(
        "--scope",
        choices=("full", "changed"),
        default="full",
        help="Which tests to run: 'full' (default, the entire suite) or "
        "'changed' (only tests touching files edited on this branch, fast "
        "feedback for feature-branch validation). 'changed' falls back to "
        "full when a subset cannot be resolved (custom suite commands, no "
        "changed files, no selectable tests).",
    )
    parser.add_argument(
        "--group",
        default=None,
        metavar="NAME",
        help="Run a named test group defined in .pi/test-config.json (e.g. "
        "'audit', 'test', 'full'). Requires groups to be defined; falls "
        "back to 'full' when no groups are configured.",
    )
    parser.add_argument(
        "--list-groups",
        action="store_true",
        help="List available named test groups and exit.",
    )
    parser.add_argument(
        "--strict-git-env",
        action="store_true",
        help="Release-gate mode: refuse to run (exit 2) when repository-"
        "override environment variables (GIT_DIR / GIT_WORK_TREE / "
        "GIT_CONFIG* / GIT_OBJECT_DIRECTORY / "
        "GIT_ALTERNATE_OBJECT_DIRECTORIES) are present, before any suite "
        "command runs. Ad-hoc runs scrub and continue; only the release "
        "gate opts in. Override with "
        f"{STRICT_GIT_ENV_OPT_OUT_ENV}=1 (loud warning, not recommended).",
    )
    parser.add_argument(
        "--target-branch",
        default=None,
        help="Base branch for changed-file detection (default: origin/dev, "
        "falling back to local 'dev' then HEAD~1).",
    )
    parser.add_argument(
        "--gate-commit",
        default=None,
        metavar="SHA",
        help="Release-gate subject binding (SA-0MVTRBT50005DUEH): the commit "
        "being pushed. The gate resolves a checkout whose HEAD is SHA with no "
        "dirty paths (in place, else a clean `git worktree list` checkout) and "
        "runs/caches the suite there, so a cache entry computed from any other "
        "state can never satisfy it. The verdict is bound to the pushed "
        "commit's tree SHA (`git rev-parse SHA^{tree}`) and stored/looked up "
        "with fingerprint_kind=\"tree\" (SA-0MW7C6BMZ00931JG), so a tree-keyed "
        "verdict from another consumer (e.g. the finish gate) is reused for the "
        "same tree. Refuses (exit 2) when no clean checkout holds SHA or its "
        "tree cannot be computed. Omitted: historical behaviour, unchanged.",
    )
    parser.add_argument(
        "--fingerprint",
        default=None,
        metavar="SHA",
        help="Bind a --scope full run to an explicit would-be-commit tree SHA "
        "(the value `git write-tree` reports for a staged change set): the "
        "run is stored and looked up with fingerprint_kind=\"tree\", keyed on "
        "that SHA and never on the loose worktree state. Fail-closed: a value "
        "that is not a usable tree SHA, or a combination that cannot bind it "
        "(--scope changed, --summary, --no-cache), is rejected with exit 2 "
        "and an actionable message — never a silent fallback to a loose "
        "worktree key. A tree entry never satisfies a worktree request (and "
        "vice versa); a tree request with no stored entry executes fresh. "
        "Omitted: historical loose-worktree keys, byte for byte.",
    )
    return parser


def run_summary(
    suites: tuple[str, ...],
    cwd: Path | None = None,
    pattern: str | None = None,
    test_type: str = DEFAULT_TEST_TYPE,
    commands: list[str] | None = None,
    group: str | None = None,
) -> dict[str, Any]:
    """Return summary lines for each suite from the cache, executing nothing.

    Suites with no cached entry report a clear miss. The result carries
    ``success`` (True only when every selected suite had a cached entry),
    ``lines`` per suite, ``scopes`` (the recorded scope of the cached entry
    per suite — ``"full"``, ``"changed"``, or ``"mixed"`` when the suite's
    commands resolve to entries with different scopes), ``types`` (the
    recorded test type per suite, same ``"mixed"``/``"missing"`` rules),
    ``interpreters`` (the recorded pytest interpreter/executable per suite,
    empty when unknown) and ``missing`` (list of suite names). A ``changed``-scope summary is NOT
    full-suite verification — consumers must treat it as partial evidence
    (the audit skill rejects changed-scope entries for full-suite ACs).
    Every suite resolves commands via ``full_suite_commands(cwd)`` filtered by
    suite name — the single source of truth (F2 AC4) — unless an explicit
    typed *commands* profile is supplied, so a repo's pinned suiteCommands
    (custom interpreter) are honoured for read-only summaries of every suite
    name (OSL-0MVNWYRQB0095WXQ). A suite with no matching command in a
    non-empty suite-command set is reported as a miss; a tool-less repo (empty
    full set) keeps the legacy name-based resolution.
    """
    cwd = cwd or REPO_ROOT
    result: dict[str, Any] = {
        "lines": {},
        "scopes": {},
        "types": {},
        "interpreters": {},
        "missing": [],
        "success": True,
    }
    for name in suites:
        if group is not None:
            suite_commands = group_commands(group, cwd) or full_suite_commands(cwd)
        elif commands is not None:
            suite_commands = filter_commands_for_suite(name, commands)
        else:
            # Resolve through ``full_suite_commands()`` — the single source of
            # truth (F2 AC4) — so a repo's pinned suiteCommands are honoured
            # for read-only cache summaries of every suite name
            # (OSL-0MVNWYRQB0095WXQ): ``--suite pytest`` in a pinned repo must
            # look up the pinned command, never the legacy bare ``pytest``
            # token. When the repo declares suite commands but none match
            # this suite, report a clear miss instead of silently succeeding
            # with nothing to look up. A tool-less repo (empty full set) keeps
            # the legacy name-based resolution unchanged.
            full = full_suite_commands(cwd)
            if full:
                suite_commands = filter_commands_for_suite(name, full)
                if not suite_commands:
                    result["missing"].append(name)
                    result["success"] = False
                    continue
            elif name == "pytest":
                suite_commands = [pytest_command()]
            elif name == "node":
                suite_commands = node_suite_commands(cwd)
            else:
                suite_commands = full
        lines: list[str] = []
        scopes: set[str] = set()
        types: set[str] = set()
        interpreters: set[str] = set()
        for cmd in suite_commands:
            entry = query_cached(
                cmd, cwd=str(cwd), ttl=CACHE_TTL_SECONDS, test_type=test_type
            )
            if entry is None:
                result["missing"].append(name)
                result["success"] = False
                continue
            lines.extend(summary_lines(entry["stdout"], entry["stderr"], pattern=pattern))
            scopes.add(entry.get("scope", "full"))
            types.add(entry.get("test_type", "full"))
            if entry.get("interpreter"):
                interpreters.add(entry["interpreter"])
        result["lines"][name] = lines
        result["scopes"][name] = "mixed" if len(scopes) > 1 else (next(iter(scopes)) if scopes else "missing")
        result["types"][name] = "mixed" if len(types) > 1 else (next(iter(types)) if types else "missing")
        result["interpreters"][name] = (
            "mixed" if len(interpreters) > 1 else next(iter(interpreters), "")
        )
    return result


def _detect_live_repo_mutation(
    project_root: Path,
    snapshot_before: dict[str, Any] | None,
) -> dict[str, Any]:
    """Evaluate the guarded run's checkout delta.

    Returns ``{"mutation": <rendered diff or None>, "excluded": [<note>, ...]}``.
    Detect-only: it never prevents a mutation, it stops a corrupted checkout
    from being reported as a green suite (and therefore from being pushed).
    ``mutation`` is ``None`` for a non-git root (no-op) and when the guard was
    stood down by an outer guard (``snapshot_before is None``).

    ``excluded`` carries the informational note for working-tree churn that was
    attributed to a concurrent session and therefore cannot fail the run
    (SA-0MUIJAHXS003M7BG AC1/AC3) — the paths, the checkout and the status
    codes, so a reviewer can tell an excluded foreign change from a clean run.
    The attribution rule itself lives in the shared ``git_sandbox`` helper, so
    the inner ``live_repo_guard`` plugin cannot drift from it.

    The surface is **every checkout the guarded run can reach**
    (WL-0MVPE2BY9001CR9O): the guarded root plus, when the run started in a
    linked worktree, its main checkout. A worktree-scoped run shares one object
    store and a symlinked ``node_modules`` with the main checkout, so a
    root-only check reported a gutted main checkout as clean (the 2026-10-26
    `packages/shared` wipe). The message names the mutated checkout and every
    mutated path in it.
    """
    if snapshot_before is None:
        return {"mutation": None, "excluded": []}
    snapshot_after = snapshot_guarded_roots(project_root)
    diff = diff_guarded_roots(snapshot_before, snapshot_after)
    excluded = excluded_churn_notes(diff)
    if not diff["changed"]:
        return {"mutation": None, "excluded": excluded}
    mutation = (
        "live-repo mutation detected: the test suite changed a guarded "
        f"checkout for {project_root}. The run is failed so a corrupted "
        "checkout cannot be pushed as green.\n" + describe_guarded_diff(diff)
    )
    return {"mutation": mutation, "excluded": excluded}


def guarded_run_all(
    cwd: Path | None = None,
    timeout: int = 600,
    *,
    suites: tuple[str, ...] = ("all",),
    use_cache: bool = True,
    force: bool = False,
    no_cache: bool = False,
    scope: str = "full",
    base_ref: str = "origin/dev",
    commands: list[str] | None = None,
    test_type: str = DEFAULT_TEST_TYPE,
    group: str | None = None,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Run the selected suites under the canonical live-repo guard ownership.

    This is the **single source of truth** for outer guard ownership
    (SA-0MUG30J5F001L57H, SA-0MUH1MJL6003767Q). Both the ``run_tests.py`` CLI
    (via :func:`main`) and ``implement.py``'s finish-gate test step call this
    function rather than arming their own guards, so there is exactly one
    implementation and no path is left unguarded.

    Ownership contract:

    - When the recursion marker ``LIVE_REPO_GUARD_ACTIVE`` is **already set**
      an outer guard owns the checkout (a nested/cascaded run): this call
      stands down — it neither snapshots nor sets/clears the marker.
    - When the marker is **not set**, this call owns the checkout: it
      snapshots the repo before the run, sets the marker for the duration of
      :func:`run_all` (so the inner conftest guard stands down and nested
      pytest invocations do not double-report), clears it in a ``finally``
      block, then detects any mutation and fails the run if the checkout
      changed.

    The result is the same dict shape as :func:`run_all`, plus a
    ``live_repo_mutation`` key when the owning guard detected a mutation.

    Args:
        cwd: Project root to run/cache against (default ``REPO_ROOT``).
        timeout: Per-command timeout in seconds.
        suites: Suite names to run (default ``("all",)``).
        use_cache: Whether to serve/store results from the per-repo cache.
        force: Bypass cache lookup (still stores).
        no_cache: Bypass cache lookup and storage.
        scope: ``"full"`` or ``"changed"``.
        base_ref: Base ref for changed-file detection.
        commands: Explicit command override (resolved type/group profile).
        test_type: Command profile name (``full`` default).
        group: Named test group from ``.pi/test-config.json``.
        fingerprint: Explicit expected fingerprint (a would-be-commit tree
            SHA) forwarded to the cache; omitted keeps loose-worktree keys.

    Returns:
        The :func:`run_all` result dict, with ``success=False`` and
        ``live_repo_mutation`` set when the owned guard detected a mutation,
        and ``live_repo_excluded`` (informational notes) when working-tree
        churn was attributed to a concurrent session and excluded from the
        verdict (SA-0MUIJAHXS003M7BG AC3).
    """
    project_root = Path(cwd or REPO_ROOT).resolve()

    # Stand down when an outer guard already owns the checkout (nested run).
    guard_active = not os.environ.get(LIVE_REPO_GUARD_ACTIVE_ENV)
    snapshot_before = (
        snapshot_guarded_roots(project_root) if guard_active else None
    )

    if guard_active:
        os.environ[LIVE_REPO_GUARD_ACTIVE_ENV] = "1"
    try:
        result = run_all(
            suites=suites,
            cwd=project_root,
            timeout=timeout,
            use_cache=use_cache,
            force=force,
            no_cache=no_cache,
            scope=scope,
            base_ref=base_ref,
            commands=commands,
            test_type=test_type,
            group=group,
            fingerprint=fingerprint,
        )
    finally:
        # Only clear the marker we set — a nested call must not clear an
        # outer guard's marker.
        if guard_active:
            os.environ.pop(LIVE_REPO_GUARD_ACTIVE_ENV, None)

    # Detect-only: fail the run when the checkout mutated, so a corrupted
    # checkout can never be reported (or pushed) as green. Armed on
    # cache-served runs too (nothing executes, but the delta is verified).
    # Working-tree churn attributed to a concurrent session is excluded from
    # the verdict but reported under ``live_repo_excluded`` (AC3) — never
    # silently, so a reviewer can tell an excluded foreign change from a
    # clean run.
    if guard_active:
        verdict = _detect_live_repo_mutation(project_root, snapshot_before)
        mutation = verdict.get("mutation")
        excluded = list(verdict.get("excluded") or [])
        if mutation is not None:
            result = {**result, "success": False, "live_repo_mutation": mutation}
        if excluded:
            result = {**result, "live_repo_excluded": excluded}

    return result


def _prune_stale_roots_before_run() -> None:
    """Prune stale pytest roots ahead of a full-scope run; never raises.

    Delegates to the shared F1 helper
    (:func:`shared.test_tmpdir.prune_stale_test_roots`, SA-0MW3EN72O005BMIP)
    and logs the removed count and freed paths at INFO (SA-0MW3ENF32006BGN1
    AC5). Fail-open (AC3): any error is caught, logged and swallowed, and the
    run proceeds unchanged — a prune problem must never change the exit
    status, cache entry or scope resolution.
    """
    try:
        removed, reason = prune_stale_test_roots()
    except Exception as exc:  # noqa: BLE001 - documented fail-open contract
        LOG.warning("Stale pytest-root prune failed (%s); continuing", exc)
        return
    if removed:
        LOG.info("Pruned %d stale pytest root(s): %s", removed, reason)
    else:
        LOG.debug("No stale pytest roots to prune (%s)", reason)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    suites = (args.suite,)
    test_type = args.test_type

    # Resolve the project root: explicit flag wins, else detect from cwd at
    # CLI time so a non-framework invocation tests that project (SA-0MSNQV9J20010LE7).
    project_root = Path(args.project_root).resolve() if args.project_root else detect_project_root()

    # Release-gate fail-fast (F4, SA-0MUIULX49001BWGG): the release path opts
    # into ``--strict-git-env`` and refuses to run when repository-override
    # vars are present. Scrubbing still happens everywhere (F3); this only
    # guards the release gate where a scrub bug would be catastrophic.
    if args.strict_git_env:
        present = repository_override_vars_present()
        if present and os.environ.get(STRICT_GIT_ENV_OPT_OUT_ENV) != "1":
            print(
                "run_tests: refusing to run the release gate with "
                "repository-override env var(s) present: "
                + ", ".join(present)
                + ". Unset them (they would be scrubbed anyway) or set "
                f"{STRICT_GIT_ENV_OPT_OUT_ENV}=1 to override (not recommended).",
                file=sys.stderr,
            )
            return 2
        if present:
            print(
                "run_tests: WARNING: repository-override env var(s) present "
                f"({', '.join(present)}); proceeding because "
                f"{STRICT_GIT_ENV_OPT_OUT_ENV}=1 (scrub still applied)",
                file=sys.stderr,
            )

    # --list-groups: print available groups and exit
    if args.list_groups:
        groups = list_groups(project_root)
        if groups is None:
            print("No named test groups defined. Define them in .pi/test-config.json.")
        else:
            for g in groups:
                print(g)
        return 0

    # --gate-commit: bind the gate to the pushed commit (SA-0MVTRBT50005DUEH)
    # before anything is resolved from the tree. The resolved checkout replaces
    # *project_root*, so every downstream step — command resolution, timeout,
    # suite execution and the cache key — describes the pushed commit rather
    # than whichever checkout git happened to run the hook in. A subject that
    # cannot be bound fails closed (exit 2, nothing executes).
    gate_evidence: dict[str, Any] | None = None
    if args.gate_commit:
        try:
            project_root, gate_evidence = resolve_gate_subject(
                args.gate_commit, project_root
            )
        except GateSubjectError as exc:
            print(f"{GATE_REFUSED_PREFIX}{exc}", file=sys.stderr)
            return 2
        _emit_gate_subject(gate_evidence)

    # --fingerprint: bind a --scope full run to an explicit would-be-commit
    # tree SHA (SA-0MW7C6AYO0062GJB). Fail-closed (AC2): an unusable value, or
    # a mode that cannot bind a tree-keyed cache entry, is rejected with exit 2
    # and an actionable message — never silently dropped, because dropping it
    # would fall back to the loose worktree key and misattribute the verdict.
    fingerprint: str | None = None
    if args.fingerprint is not None:
        if args.scope != "full":
            print(
                "run_tests: --fingerprint binds a --scope full run to an "
                f"explicit tree SHA and cannot bind --scope {args.scope}: a "
                "partial run is not full-suite evidence for that tree. Drop "
                "--scope or omit --fingerprint.",
                file=sys.stderr,
            )
            return 2
        if args.summary:
            print(
                "run_tests: --fingerprint stores and queries a cache entry and "
                "cannot be combined with --summary, which reads only loose "
                "worktree entries (tree-bound entries are deliberately "
                "invisible to it). Omit --fingerprint for a summary, or run "
                "the suite to produce the tree-keyed verdict.",
                file=sys.stderr,
            )
            return 2
        if args.no_cache:
            print(
                "run_tests: --fingerprint binds a stored cache entry and "
                "cannot be combined with --no-cache, which neither looks up "
                "nor stores one. Omit --fingerprint or drop --no-cache.",
                file=sys.stderr,
            )
            return 2
        try:
            fingerprint = resolve_explicit_fingerprint(
                args.fingerprint, project_root
            )
        except FingerprintRejected as exc:
            print(f"run_tests: --fingerprint: {exc}", file=sys.stderr)
            return 2

    # A gate run is tree-keyed (SA-0MW7C6BMZ00931JG): the pushed commit's tree
    # SHA is the explicit fingerprint, so the verdict is stored and looked up
    # in the same ``fingerprint_kind="tree"`` namespace as the finish gate's
    # tree verdicts — a verdict recorded for the same tree by either consumer
    # serves the other. The derived tree is authoritative; a conflicting
    # explicit --fingerprint is refused rather than silently overridden.
    if gate_evidence is not None:
        gate_tree = str(gate_evidence["tree"])
        if fingerprint is not None and fingerprint != gate_tree:
            print(
                f"run_tests: --gate-commit binds the run to pushed tree "
                f"{gate_tree}, but --fingerprint names {fingerprint}. They must "
                "match; omit --fingerprint (the pushed commit's tree is the "
                "gate's fingerprint).",
                file=sys.stderr,
            )
            return 2
        fingerprint = gate_tree

    # --group: validate group name and route to group-based execution
    group_name = args.group
    if group_name is not None:
        groups = list_groups(project_root)
        if groups is None:
            print(
                f"run_tests: group '{group_name}' specified but no groups are "
                "defined in .pi/test-config.json.",
                file=sys.stderr,
            )
            return 2
        if group_name not in groups:
            print(
                f"run_tests: unknown group '{group_name}'. Available groups: "
                f"{', '.join(groups)}.",
                file=sys.stderr,
            )
            return 2
        # Resolve group commands and run them
        group_cmds = group_commands(group_name, project_root)
        if group_cmds is None:
            print(f"run_tests: group '{group_name}' has no resolvable commands.", file=sys.stderr)
            return 2
        # Override test_type to indicate group mode
        test_type = f"group:{group_name}"
        override_commands = group_cmds
    else:
        override_commands = None

    # When --group is used, skip type validation/resolution (group commands
    # are already resolved above; test_type is "group:<name>" which is not in
    # the allowed minimum types list).
    if group_name is None:
        # Validate the requested type against the allowed set (minimum set plus
        # any locally-defined types) and report the full list on error (AC1/AC3).
        try:
            allowed_types = allowed_test_types(project_root)
        except SkillExtensionError as exc:
            print(f"run_tests: {exc}", file=sys.stderr)
            return 2
        if test_type not in allowed_types:
            print(
                f"run_tests: unknown test type '{test_type}'. Allowed types: "
                f"{', '.join(allowed_types)}.",
                file=sys.stderr,
            )
            return 2

        # Resolve the type's command profile once, failing fast with a clear
        # diagnostic — never silently substituting the full suite (AC2/AC4).
        try:
            type_commands = resolve_type_commands(project_root, test_type)
        except (TypeResolutionError, SkillExtensionError) as exc:
            print(f"run_tests: {exc}", file=sys.stderr)
            return 2

    # Legacy path (AC5): a full-type run with no local override lets run_suite
    # resolve by suite name exactly as before. Otherwise the resolved profile
    # is passed explicitly and narrowed to --suite.
    # (When --group was used, override_commands is already set above.)
    if test_type == DEFAULT_TEST_TYPE and type_commands == full_suite_commands(project_root):
        if override_commands is None:
            override_commands = None
    else:
        if override_commands is None:
            override_commands = filter_commands_for_suite(args.suite, type_commands)
            if not override_commands:
                print(
                    f"run_tests: test type '{test_type}' defines no commands for "
                    f"the '{args.suite}' suite.",
                    file=sys.stderr,
                )
                return 2

    # Per-command timeout: explicit --timeout wins, else the extension file's
    # timeoutPerCommand (F2 AC1), else the default 600.
    timeout = args.timeout or suite_timeout_per_command(project_root) or 600

    with Timer("run_tests") as _root_timer:
        if args.summary:
            with Timer("run_summary"):
                summary = run_summary(
                    suites,
                    cwd=project_root,
                    pattern=args.summary_grep,
                    test_type=test_type,
                    commands=override_commands,
                    group=group_name,
                )
            if args.json:
                summary["timing"] = _root_timer.to_dict()
                print(json.dumps(summary, indent=2))
            else:
                for name, lines in summary["lines"].items():
                    if name in summary["missing"]:
                        print(f"{name}: no cached result — run the suite first or use --force")
                    else:
                        scope = summary["scopes"].get(name, "full")
                        recorded_type = summary["types"].get(name, test_type)
                        interpreter = summary["interpreters"].get(name, "")
                        interpreter_info = (
                            f", pytest interpreter: {interpreter}" if interpreter else ""
                        )
                        print(
                            f"{name} summary ({scope} scope, "
                            f"type={recorded_type}{interpreter_info}):"
                        )
                        for line in lines:
                            print(f"  {line}")
                print(_root_timer.render(), file=sys.stderr)
            return 0 if summary["success"] else 1

        # Live-repo guard ownership is centralised in ``guarded_run_all``
        # (SA-0MUH1MJL6003767Q): it snapshots the checkout, sets the recursion
        # marker so the inner conftest guard stands down, runs the suites, then
        # fails the run if the checkout mutated. Delegating here keeps the CLI
        # and ``implement.py``'s finish gate on exactly one guard
        # implementation (no double-guard, no path left unguarded).
        result = guarded_run_all(
            project_root,
            timeout=timeout,
            suites=suites,
            use_cache=not args.no_cache,
            force=args.force,
            no_cache=args.no_cache,
            scope=args.scope,
            base_ref=args.target_branch or "origin/dev",
            commands=override_commands,
            test_type=test_type,
            group=group_name,
            fingerprint=fingerprint,
        )

        if gate_evidence is not None:
            _emit_gate_verdict(gate_evidence, result)
            # The same provenance is carried in the JSON report under
            # ``gate`` so a served verdict (hit/miss plus the reused entry's
            # age) is machine-readable, not only echoed on stderr
            # (SA-0MW7C6BMZ00931JG AC3).
            result["gate"] = _gate_verdict_facts(gate_evidence, result)

        if args.rerun_failures and result["failures"]:
            with Timer("rerun_failures"):
                result["failures"] = rerun_failures(result["failures"], timeout=timeout)
            result["success"] = all(r["success"] for r in result["suites"].values()) and not result["failures"]

        if args.json:
            result["timing"] = _root_timer.to_dict()
            print(json.dumps(result, indent=2))
        else:
            for name, suite_result in result["suites"].items():
                status = "PASS" if suite_result["success"] else "FAIL"
                cached = " [cached]" if suite_result.get("cached") else ""
                scope = f" [{suite_result.get('scope', 'full')} scope]"
                type_info = f" [type {suite_result.get('type', test_type)}]"
                lane_info = f" [{suite_result.get('slot_lane', 'full')} lane]"
                interpreter = suite_result.get("interpreter") or ""
                interpreter_info = f" [pytest interpreter: {interpreter}]" if interpreter else ""
                print(
                    f"{name}: {status}{cached}{scope}{type_info}{lane_info}{interpreter_info} "
                    f"({suite_result['command']})"
                )
                if (suite_result.get("slot_wait_seconds") or 0.0) >= 1.0:
                    print(
                        f"  queued {suite_result['slot_wait_seconds']:.1f}s for a "
                        f"{suite_result.get('slot_lane', 'full')}-lane test slot "
                        f"(wait, not execution — see elapsed in the facts below)"
                    )
                if suite_result.get("notice"):
                    print(f"  notice: {suite_result['notice']}")
                if not suite_result["success"]:
                    print(f"  facts: {_render_suite_facts(suite_result)}")
                for failure in suite_result["failures"]:
                    print(f"  FAILED: {failure['test_name']}")
            for notice in result["notices"]:
                print(f"notice: {notice}")
            for note in result.get("live_repo_excluded") or []:
                print(f"note: {note}")
            if result.get("live_repo_mutation"):
                print("ERROR: " + result["live_repo_mutation"], file=sys.stderr)
            print(_root_timer.render(), file=sys.stderr)

    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
