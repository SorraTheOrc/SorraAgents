#!/usr/bin/env python3
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

Emits structured per-failure records (test_name, stdout_excerpt, stack_trace)
compatible with the triage skill's check_or_create.py input.

Usage:
  run_tests.py [--suite pytest|node|all] [--json] [--parent-work-item-id ID] [--rerun-failures]
  run_tests.py --summary [--summary-grep PATTERN]   (read cached summary lines, no execution)
  run_tests.py --force | --no-cache                (bypass the cache)

Exit codes:
  0 - all suites passed
  1 - at least one test failed (or --summary cache miss)
  2 - runner error (missing suite binary etc.)
"""  # noqa: EXE001

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_SKILLS_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_ROOT_STR = str(_SKILLS_ROOT)
if _SKILLS_ROOT_STR in sys.path:
    sys.path.remove(_SKILLS_ROOT_STR)
sys.path.insert(0, _SKILLS_ROOT_STR)

from test_cache import (
    DEFAULT_TTL_SECONDS,
    query_cached,
    run_cached,
    summary_lines,
)
from test_runner import (
    canonicalize_quiet_test_command,
    executable_test_command,
)

REPO_ROOT = _SKILLS_ROOT.parent

# Cache TTL exposed as a module constant so CLI tests can backdate entries.
CACHE_TTL_SECONDS = DEFAULT_TTL_SECONDS

# Canonical pytest command (stable form for cache keying and cross-consumer
# reuse). The executable is resolved at run time via executable_test_command
# so a shell without ~/.local/bin on PATH still runs the suite
# (SA-0MSQ012QG005N22S).
PYTEST_CMD = canonicalize_quiet_test_command("pytest")
NODE_SUITE_DIRS = ("tests/node", "tests/cli", "tests/unit")

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

_FAILED_RE = re.compile(r"^FAILED\s+(.+?)\s+-\s+(.*)$", re.MULTILINE)
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

    return selected


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
# Failure parsing
# ---------------------------------------------------------------------------


def parse_pytest_failures(output: str) -> list[dict[str, str]]:
    """Parse pytest ``-r a`` output into per-failure structured records.

    Each record contains ``test_name``, ``stdout_excerpt`` and ``stack_trace``
    in the shape expected by triage check_or_create.py.
    """
    records: list[dict[str, str]] = []
    failed = list(_FAILED_RE.finditer(output))
    for match in failed:
        test_name = match.group(1).strip()
        # Extract the traceback section for this test from the FAILURES block.
        stack_trace = _extract_pytest_section(output, test_name)
        excerpt = stack_trace[:1000] if stack_trace else match.group(2).strip()
        records.append(
            {
                "test_name": test_name,
                "stdout_excerpt": excerpt,
                "stack_trace": stack_trace or excerpt,
            }
        )
    return records


def _extract_pytest_section(output: str, test_name: str) -> str:
    """Return the pytest FAILURES section body for a given test name."""
    # The section header is the test node id's tail (function name or full id).
    tail = test_name.split("::")[-1]
    lines = output.splitlines()
    section_start = None
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line.strip())
        if not m:
            continue
        header = m.group(1).strip()
        if header == tail or header == test_name or header.endswith("::" + tail):
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


def _run_cmd(cmd: list[str], cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess:
    """Run a suite command capturing stdout/stderr."""
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _cached_runner(command: str, cwd: str, timeout: int) -> subprocess.CompletedProcess:
    """Adapter exposing _run_cmd to the cache runner protocol.

    Uses shlex.split (not str.split) so quoted args like
    ``node --test "tests/node/**/*.mjs"`` pass the glob without literal
    quotes — otherwise node matches zero files and the suite trivially
    "passes" with 0 tests (pre-existing bug fixed with SA-0MSGN5OJ4002OZKY).

    The pytest executable is resolved before spawning so a shell without
    ``~/.local/bin`` on PATH still runs the suite (SA-0MSQ012QG005N22S);
    the canonical command (cache key) is unchanged.
    """
    executable = executable_test_command(command)
    return _run_cmd(shlex.split(executable), cwd=Path(cwd), timeout=timeout)


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
    partial run for a full one. The result always carries ``scope``
    ("full"|"changed") and, for changed scope, ``changed_files`` (the file
    set that drove selection). The scope is recorded in the per-repo test
    cache metadata (``run_cached(scope=...)``) so read-only consumers can
    reject partial runs as full-suite evidence.

    Returns a dict with ``success``, ``returncode``, ``failures``, ``command``,
    ``cached``, ``scope`` and (on missing binary) ``notice``.
    """
    cwd = cwd or REPO_ROOT
    resolvable_scope = scope
    changed_files: set[str] = set()
    if commands is None:
        if scope == "changed":
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
            if name == "pytest":
                commands = [pytest_command()]
            elif name == "node":
                commands = node_suite_commands()
            elif name == "all":
                commands = full_suite_commands(cwd)
            else:
                raise ValueError(f"unknown suite: {name}")
            resolvable_scope = "full"
    command = " && ".join(commands)

    all_failures: list[dict[str, str]] = []
    overall_returncode = 0
    cached_flags: list[bool] = []
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
                )
                proc = SimpleNamespace(
                    stdout=run["stdout"], stderr=run["stderr"], returncode=run["exit_code"]
                )
                cached_flags.append(run["cached"])
            else:
                proc = _run_cmd(
                    shlex.split(executable_test_command(cmd)),
                    cwd=cwd,
                    timeout=timeout,
                )
                cached_flags.append(False)
        except FileNotFoundError as exc:
            return {
                "success": False,
                "returncode": None,
                "command": command,
                "failures": [],
                "notice": f"command not found: {exc.filename}",
                "scope": resolvable_scope,
                "changed_files": sorted(changed_files) if resolvable_scope == "changed" else [],
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "returncode": None,
                "command": command,
                "failures": [],
                "notice": f"suite timed out after {timeout}s: {name}",
                "scope": resolvable_scope,
                "changed_files": sorted(changed_files) if resolvable_scope == "changed" else [],
            }

        output = f"{proc.stdout}\n{proc.stderr}"
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
        "changed_files": sorted(changed_files) if resolvable_scope == "changed" else [],
        "notice": "",
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
    """
    results: dict[str, Any] = {}
    all_failures: list[dict[str, str]] = []
    notices: list[str] = []
    resolved_scopes: list[str] = []
    for name in suites:
        result = run_suite(
            name,
            cwd=cwd,
            timeout=timeout,
            use_cache=use_cache,
            force=force,
            no_cache=no_cache,
            scope=scope,
            base_ref=base_ref,
        )
        results[name] = result
        resolved_scopes.append(result["scope"])
        for failure in result["failures"]:
            all_failures.append({**failure, "suite": name})
        if result.get("notice"):
            notices.append(result["notice"])
    return {
        "success": all(r["success"] for r in results.values()),
        "suites": results,
        "failures": all_failures,
        "notices": notices,
        "scope": scope,
        "resolved_scopes": resolved_scopes,
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
            proc = _run_cmd(
                shlex.split(executable_test_command(cmd)),
                cwd=cwd or REPO_ROOT,
                timeout=timeout,
            )
        except FileNotFoundError:
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
) -> dict[str, Any]:
    """Return summary lines for each suite from the cache, executing nothing.

    Suites with no cached entry report a clear miss. The result carries
    ``success`` (True only when every selected suite had a cached entry),
    ``lines`` per suite, ``scopes`` (the recorded scope of the cached entry
    per suite — ``"full"``, ``"changed"``, or ``"mixed"`` when the suite's
    commands resolve to entries with different scopes), and ``missing``
    (list of suite names). A ``changed``-scope summary is NOT full-suite
    verification — consumers must treat it as partial evidence (the audit
    skill rejects changed-scope entries for full-suite ACs). The ``"all"``
    suite resolves commands via ``full_suite_commands(cwd)`` — the single
    source of truth (F2 AC4).
    """
    cwd = cwd or REPO_ROOT
    result: dict[str, Any] = {"lines": {}, "scopes": {}, "missing": [], "success": True}
    for name in suites:
        if name == "pytest":
            commands = [pytest_command()]
        elif name == "node":
            commands = node_suite_commands()
        else:
            commands = full_suite_commands(cwd)
        lines: list[str] = []
        scopes: set[str] = set()
        for cmd in commands:
            entry = query_cached(cmd, cwd=str(cwd), ttl=CACHE_TTL_SECONDS)
            if entry is None:
                result["missing"].append(name)
                result["success"] = False
                continue
            lines.extend(summary_lines(entry["stdout"], entry["stderr"], pattern=pattern))
            scopes.add(entry.get("scope", "full"))
        result["lines"][name] = lines
        result["scopes"][name] = "mixed" if len(scopes) > 1 else (next(iter(scopes)) if scopes else "missing")
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    suites = (args.suite,)

    # Resolve the project root: explicit flag wins, else detect from cwd at
    # CLI time so a non-framework invocation tests that project (SA-0MSNQV9J20010LE7).
    project_root = Path(args.project_root).resolve() if args.project_root else detect_project_root()

    # Per-command timeout: explicit --timeout wins, else the extension file's
    # timeoutPerCommand (F2 AC1), else the default 600.
    timeout = args.timeout or suite_timeout_per_command(project_root) or 600

    if args.summary:
        summary = run_summary(suites, cwd=project_root, pattern=args.summary_grep)
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            for name, lines in summary["lines"].items():
                if name in summary["missing"]:
                    print(f"{name}: no cached result — run the suite first or use --force")
                else:
                    scope = summary["scopes"].get(name, "full")
                    print(f"{name} summary ({scope} scope):")
                    for line in lines:
                        print(f"  {line}")
        return 0 if summary["success"] else 1

    result = run_all(
        suites=suites,
        cwd=project_root,
        timeout=timeout,
        use_cache=not args.no_cache,
        force=args.force,
        no_cache=args.no_cache,
        scope=args.scope,
        base_ref=args.target_branch or "origin/dev",
    )

    if args.rerun_failures and result["failures"]:
        result["failures"] = rerun_failures(result["failures"], timeout=timeout)
        result["success"] = all(r["success"] for r in result["suites"].values()) and not result["failures"]

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for name, suite_result in result["suites"].items():
            status = "PASS" if suite_result["success"] else "FAIL"
            cached = " [cached]" if suite_result.get("cached") else ""
            scope = f" [{suite_result.get('scope', 'full')} scope]"
            print(f"{name}: {status}{cached}{scope} ({suite_result['command']})")
            if suite_result.get("notice"):
                print(f"  notice: {suite_result['notice']}")
            for failure in suite_result["failures"]:
                print(f"  FAILED: {failure['test_name']}")
        for notice in result["notices"]:
            print(f"notice: {notice}")

    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
