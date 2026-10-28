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

Concurrent suite executions are bounded host-wide via the shared "test"
semaphore (skill/shared/process_semaphore.py): TEST_MAX_CONCURRENCY (default
2) ceilings concurrent runs, TEST_LOCK_TIMEOUT (default 600s) bounds the wait
for a free slot. Only ACTUAL executions acquire a slot — cache hits are
served without executing and never block on the semaphore
(SA-0MTG5U75A001F1RG).

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

Exit codes:
  0 - all suites passed
  1 - at least one test failed (or --summary cache miss)
  2 - runner error (missing suite binary etc.)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_SKILLS_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_ROOT_STR = str(_SKILLS_ROOT)
if _SKILLS_ROOT_STR in sys.path:
    sys.path.remove(_SKILLS_ROOT_STR)
sys.path.insert(0, _SKILLS_ROOT_STR)

from shared.git_sandbox import (
    describe_guarded_diff,
    diff_guarded_roots,
    excluded_churn_notes,
    snapshot_guarded_roots,
)
from shared.process_semaphore import Semaphore, nested_semaphore_dir  # noqa: F401
from shared.skill_extensions import (
    DATA_FILENAME,
    SkillExtensionError,
    load_extension,
)
from shared.timing import Timer
from test_cache import (
    DEFAULT_TTL_SECONDS,
    Runner,
    _default_runner,
    query_cached,
    run_cached,
    summary_lines,
)
from test_runner import (
    canonicalize_quiet_test_command,
    executable_test_command,
    is_pytest_command,
    log_repository_override_scrub,
    repository_override_vars_present,
    resolve_pytest_executable,
    subprocess_env_with_scrubbed_overrides,
)

REPO_ROOT = _SKILLS_ROOT.parent

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
TEST_SEMAPHORE_NAME = "test"
TEST_MAX_CONCURRENCY_ENV = "TEST_MAX_CONCURRENCY"
TEST_MAX_CONCURRENCY_DEFAULT = 2
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
#: command-not-found, suite timeout. Consumers (e.g. ``implement.py``'s finish
#: gate) classify on the code — never on the human-readable notice text — so
#: rewording a notice can never silently reclassify a genuine test failure as
#: an infrastructure condition.
NOTICE_SLOT_BUSY = "slot_busy"
NOTICE_COMMAND_NOT_FOUND = "command_not_found"
NOTICE_SUITE_TIMEOUT = "suite_timeout"

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

TRACKED_SOURCE_EXTENSIONS = (".py", ".mjs", ".js", ".ts")


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

    Untracked files are not included (they have no diff and would be
    reported only by ``git status``, not a diff). The merge-base is used so
    only files changed on the current branch since it forked from the base
    are returned — files changed on the base after the fork point are not
    attributed to this branch.

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
    )
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}

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
       ``tests/test_foo.py`` itself; any ``test_*.py`` / ``*_test.py`` in the
       same directory tree as a non-test change whose basename matches the
       changed file's stem.
    2. **Import-graph expansion**: AST-scan every test file once; for each
       changed module (dotted name derived from its path relative to the
       project root), find test files whose import statements reference the
       changed module OR any module that (transitively) imports it. This
       catches indirect breakage — e.g. ``utils.py`` changed →
       ``test_report.py`` imported via a chain.

    ``changed_files`` defaults to :func:`compute_changed_files` against the
    calling repo. Non-Python changes (README, yaml, …) never map to tests
    (they return an empty selection) unless they are themselves test files.

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

    # All test files under tests/test dirs (matching run_tests conventions).
    test_dirs = [d for d in ("tests", "test") if (root / d).is_dir()]
    all_tests: set[str] = set()
    for d in test_dirs:
        all_tests.update(
            str(p.relative_to(root))
            for p in (root / d).rglob("*.py")
        )
    # Node suite dirs also carry test files (tests/node|cli|unit/**/*.mjs)
    # — included so a changed source can select its node tests by convention.
    for d in NODE_SUITE_DIRS:
        node_dir = root / d
        if not node_dir.is_dir():
            continue
        for p in node_dir.rglob("*.mjs"):
            if _is_test_file(Path(str(p.relative_to(root)))):
                all_tests.add(str(p.relative_to(root)))

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

        # Direct convention: changed_stem → test_<stem>.py in same tree
        for test in all_tests:
            test_rel = Path(test)
            if test_rel.name in {
                f"test_{rel.stem}.py",
                f"{rel.stem}_test.py",
                f"test_{rel.stem}.mjs",
                f"{rel.stem}_test.mjs",
                f"{rel.stem}.test.mjs",
            }:
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
    """True for names matching test-file conventions (test_*.py / *_test.py)."""
    name = rel.name
    return (
        name.startswith("test_")
        or name.endswith(("_test.py", ".test.mjs", "_test.mjs"))
    )


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


def changed_scope_commands(
    project_root: Path | None = None,
    base_ref: str = "origin/dev",
    changed_files: set[str] | None = None,
) -> list[str] | None:
    """Return the test commands for changed-file scope, or None for full scope.

    Returns None (meaning "fall back to the full suite") when:

    - The repo declares custom ``suiteCommands`` in ``.pi/test-config.json``
      (not introspectable → cannot select a subset).
    - No changed files are found (nothing to select → full scope).
    - The resolved changed-file selection is empty (e.g. only non-source
      changes like README edits).
    - The repo has no test tooling of a kind we can subset (no pytest, no
      node suite dirs).

    When a selection IS possible, returns a list with, per applicable suite:

    - pytest: ``pytest <selected test files>`` (canonicalized quiet form).
    - node: ``node --test <selected node test files>`` per node suite dir.

    The selected test files are always passed explicitly so the partial run
    is deterministic and cache-keyed distinctly from the full suite.

    *changed_files* may be passed in to avoid a second ``git diff`` when the
    caller already computed it (single source for the fallback warning).
    """
    root = Path(project_root or REPO_ROOT).resolve()

    # Custom suite commands are not introspectable — fall back to full scope.
    if extension_suite_commands(root) is not None:
        return None

    changed = (
        changed_files
        if changed_files is not None
        else compute_changed_files(root, base_ref=base_ref)
    )
    if not changed:
        return None

    selected = map_changed_to_tests(root, changed)
    # Anything changed that is itself a test file is already in `selected`;
    # leaf-only changes (e.g. a lone docs edit) → full scope.
    if not selected:
        return None

    # Defensive existence filter: never emit a command referencing a path that
    # is absent from the worktree (deleted/renamed away). If filtering leaves
    # nothing selectable, fall back to full scope rather than emitting a
    # degenerate command that would exit 4 (LP-0MTZYRTNF0092JKW).
    selected = {f for f in selected if (root / f).exists()}
    if not selected:
        return None

    pytest_files = sorted(f for f in selected if f.endswith(".py") and not f.endswith(".mjs"))
    node_files = sorted(f for f in selected if f.endswith(".mjs"))

    commands: list[str] = []
    if pytest_files and repo_has_pytest_suite(root):
        file_args = " ".join(shlex.quote(f) for f in pytest_files)
        commands.append(canonicalize_quiet_test_command(f"pytest {file_args}"))

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

    return commands or None


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
    """Select the smallest sufficient group from changed files.

    Maps touched paths to groups via the ``paths`` spec in each group's
    definition.  The **smallest** (most specific, fewest commands) group
    wins.  When multiple groups match, the one with the fewest commands is
    selected.  When no group matches, returns ``None`` so the caller falls
    back to ``full``.

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

    # Find all groups that match any changed file.
    matching: dict[str, int] = {}
    for changed_file in changed_files:
        for group_name, spec in groups.items():
            if group_name == "full":
                continue
            if _paths_match_spec(changed_file, spec["paths"]):
                matching[group_name] = matching.get(group_name, 0) + len(spec["commands"])

    if not matching:
        return None

    # Return the group with the fewest commands (most specific).
    return min(matching, key=matching.get)


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
    3. **node** — one command per existing ``tests/{unit,node,cli}`` dir
       (SA-0MSJELL44009XYIL).
    4. **npm-test convention** (F2 AC2): when neither pytest nor any node
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
    """Resolve the test-run concurrency ceiling.

    Precedence: ``TEST_MAX_CONCURRENCY`` env var > default 2. Values below
    1 are clamped to 1; an invalid (non-integer) value is ignored with a
    warning so a misconfigured environment cannot break test runs. The
    ceiling bounds concurrent suite executions host-wide via the shared
    "test" semaphore — a separate namespace from the audit semaphore
    (SA-0MTG5U75A001F1RG AC1/AC2/AC3).
    """
    env_value = os.environ.get(TEST_MAX_CONCURRENCY_ENV)
    if env_value:
        try:
            return max(1, int(env_value))
        except ValueError:
            print(
                f"Warning: invalid {TEST_MAX_CONCURRENCY_ENV} value "
                f"{env_value!r}; using default ceiling",
                file=sys.stderr,
            )
    return TEST_MAX_CONCURRENCY_DEFAULT


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
def _test_concurrency_slot() -> Iterator[None]:
    """Hold a shared test-run slot for the duration of an actual execution.

    Bounds concurrent test suite executions via the shared "test"
    semaphore (ceiling ``TEST_MAX_CONCURRENCY``, bounded wait
    ``TEST_LOCK_TIMEOUT``). Only ACTUAL executions acquire a slot — a
    cached result is served without executing and never blocks on the
    semaphore, so cache behavior is unchanged (SA-0MTG5U75A001F1RG AC4).

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
    max_workers = _test_semaphore_max_workers()
    lock_timeout = _test_lock_timeout()
    sem = Semaphore(
        TEST_SEMAPHORE_NAME,
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
            f"{lock_timeout:.0f}s (TEST_MAX_CONCURRENCY={max_workers}) "
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

    ``NODE_OPTIONS`` and every other unrelated variable are preserved, so
    consumer-provided vitest/NODE_OPTIONS plumbing keeps working.
    """
    env = subprocess_env_with_scrubbed_overrides()
    env[TEST_VITEST_MAX_WORKERS_ENV] = str(_test_vitest_max_workers())
    return env


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


def _cached_runner(command: str, cwd: str, timeout: int) -> subprocess.CompletedProcess:
    """Adapter exposing _run_cmd to the cache runner protocol.

    Uses shlex.split (not str.split) so quoted args like
    ``node --test "tests/node/**/*.mjs"`` pass the glob without literal
    quotes — otherwise node matches zero files and the suite trivially
    "passes" with 0 tests (pre-existing bug fixed with SA-0MSGN5OJ4002OZKY).

    The pytest executable is resolved before spawning so the suite runs under
    the runner's interpreter (or the first probed candidate that can run
    pytest) instead of a PATH ``pytest`` bound to a foreign interpreter
    (SA-0MVK154IE007OS3D); the canonical command (cache key) is unchanged.
    """
    executable = executable_test_command(command)
    with _test_concurrency_slot():
        return _run_cmd(shlex.split(executable), cwd=Path(cwd), timeout=timeout)


def paced_runner(
    runner: Runner | None = None,
    on_wait: Callable[[float, float], None] | None = None,
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

    This is the single pacer entry point other code paths (e.g.
    ``implement.py``'s finish-gate suites, SA-0MUKHCO02009EQFG) use to route
    their executions through the same host-wide ``"test"`` namespace as
    ``run_tests.py`` (SA-0MTG5U75A001F1RG). A caller that does not pace keeps
    its previous unpaced behaviour, so this is opt-in.

    Raises:
        TestConcurrencyTimeout: when no slot frees within ``TEST_LOCK_TIMEOUT``.
    """
    if runner is None:
        runner = _default_runner

    def _paced(command: str, cwd: str, timeout: int) -> subprocess.CompletedProcess:
        queued_at = time.time()
        with _test_concurrency_slot():
            if on_wait is not None:
                on_wait(queued_at, time.time() - queued_at)
            return runner(command, cwd, timeout)

    return _paced


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
    ``_run_tests_via_test_skill``.

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

    Returns a dict with ``success``, ``returncode``, ``failures``, ``command``,
    ``cached``, ``scope``, ``type``; on a failure WITHOUT structured failures a
    ``notice`` (human-readable) plus a stable ``notice_code`` and bounded
    ``stdout``/``stderr`` tails (WL-0MVGUT787000LNDG).
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
            # subset (custom suiteCommands, no selection) falls back to full.
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

    all_failures: list[dict[str, str]] = []
    overall_returncode = 0
    cached_flags: list[bool] = []
    interpreters: set[str] = set()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    for cmd in commands:
        try:
            if use_cache:
                run = run_cached(
                    cmd,
                    cwd=str(cwd),
                    force=force,
                    no_cache=no_cache,
                    ttl=CACHE_TTL_SECONDS,
                    timeout=timeout,
                    runner=_cached_runner,
                    scope=resolvable_scope,
                    test_type=test_type,
                )
                proc = SimpleNamespace(
                    stdout=run["stdout"], stderr=run["stderr"], returncode=run["exit_code"]
                )
                cached_flags.append(run["cached"])
                if run.get("interpreter"):
                    interpreters.add(run["interpreter"])
            else:
                with _test_concurrency_slot():
                    proc = _run_cmd(
                        shlex.split(executable_test_command(cmd)),
                        cwd=cwd,
                        timeout=timeout,
                    )
                cached_flags.append(False)
                if is_pytest_command(cmd):
                    interpreters.add(resolve_pytest_executable())
        except FileNotFoundError as exc:
            return {
                "success": False,
                "returncode": None,
                "command": command,
                "failures": [],
                "notice": f"command not found: {exc.filename}",
                "notice_code": NOTICE_COMMAND_NOT_FOUND,
                "stdout": _output_tail("\n".join(stdout_parts)),
                "stderr": _output_tail("\n".join(stderr_parts)),
                "scope": resolvable_scope,
                "type": test_type,
                "interpreter": (
                    "mixed" if len(interpreters) > 1 else next(iter(interpreters), "")
                ),
                "changed_files": sorted(changed_files) if resolvable_scope == "changed" else [],
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "returncode": None,
                "command": command,
                "failures": [],
                "notice": f"suite timed out after {timeout}s: {name}",
                "notice_code": NOTICE_SUITE_TIMEOUT,
                "stdout": _output_tail("\n".join(stdout_parts)),
                "stderr": _output_tail("\n".join(stderr_parts)),
                "scope": resolvable_scope,
                "type": test_type,
                "interpreter": (
                    "mixed" if len(interpreters) > 1 else next(iter(interpreters), "")
                ),
                "changed_files": sorted(changed_files) if resolvable_scope == "changed" else [],
            }
        except TestConcurrencyTimeout as exc:
            # Concurrency ceiling stayed saturated past the bounded wait:
            # report a clear failure notice (never a crash, never fail-fast
            # before the bound) — SA-0MTG5U75A001F1RG AC1.
            return {
                "success": False,
                "returncode": None,
                "command": command,
                "failures": [],
                "notice": str(exc),
                "notice_code": NOTICE_SLOT_BUSY,
                "stdout": _output_tail("\n".join(stdout_parts)),
                "stderr": _output_tail("\n".join(stderr_parts)),
                "scope": resolvable_scope,
                "type": test_type,
                "interpreter": (
                    "mixed" if len(interpreters) > 1 else next(iter(interpreters), "")
                ),
                "changed_files": sorted(changed_files) if resolvable_scope == "changed" else [],
            }

        output = f"{proc.stdout}\n{proc.stderr}"
        stdout_parts.append(proc.stdout or "")
        stderr_parts.append(proc.stderr or "")
        if "pytest" in cmd:
            failures = parse_pytest_failures(output)
        else:
            failures = parse_node_failures(output)
        all_failures.extend(failures)
        if proc.returncode != 0:
            overall_returncode = proc.returncode

    return {
        "success": overall_returncode == 0 and not all_failures,
        "returncode": overall_returncode,
        "command": command,
        "failures": all_failures,
        "cached": use_cache and all(cached_flags) if cached_flags else False,
        "scope": resolvable_scope,
        "type": test_type,
        "interpreter": "mixed" if len(interpreters) > 1 else next(iter(interpreters), ""),
        "changed_files": sorted(changed_files) if resolvable_scope == "changed" else [],
        "notice": "",
        "notice_code": "",
        "stdout": _output_tail("\n".join(stdout_parts)),
        "stderr": _output_tail("\n".join(stderr_parts)),
    }


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
    """
    results: dict[str, Any] = {}
    all_failures: list[dict[str, str]] = []
    notices: list[str] = []
    notice_codes: list[str] = []
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    resolved_scopes: list[str] = []
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
                )
            results[name] = result
            resolved_scopes.append(result["scope"])
            for failure in result["failures"]:
                all_failures.append({**failure, "suite": name})
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
        "notices": notices,
        "notice_codes": notice_codes,
        "stdout": "\n".join(stdout_parts),
        "stderr": "\n".join(stderr_parts),
        "scope": scope,
        "type": test_type,
        "resolved_scopes": resolved_scopes,
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
            with _test_concurrency_slot():
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


def build_parser() -> argparse.ArgumentParser:
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
        )

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
                interpreter = suite_result.get("interpreter") or ""
                interpreter_info = f" [pytest interpreter: {interpreter}]" if interpreter else ""
                print(
                    f"{name}: {status}{cached}{scope}{type_info}{interpreter_info} "
                    f"({suite_result['command']})"
                )
                if suite_result.get("notice"):
                    print(f"  notice: {suite_result['notice']}")
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
