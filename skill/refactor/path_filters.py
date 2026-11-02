"""Scan-scope path filtering for the refactor skill.

The refactor skill must analyse only code that belongs to the project being
inspected.  Vendored code, npm-installed packages and anything the project's
``.gitignore`` excludes are external to the project and must be dropped
before auto-fix and smell detection so the report and the work items it
creates stay actionable.

:func:`filter_scannable_paths` applies, in order:

1. the source-extension check (full-project scans only);
2. the default external directory exclusion (:data:`DEFAULT_EXCLUDED_DIRS`,
   matched at any depth);
3. the project's config ``exclude`` globs/directories (``.refactor.json``);
4. the project's config ``include`` overrides — a re-include beats every
   exclusion: the defaults, the config ``exclude`` entries *and*
   ``.gitignore``;
5. ``.gitignore`` filtering, for tracked files too, through a **single**
   batched ``git check-ignore --no-index --stdin -z`` call.

Usage:

    from refactor.path_filters import filter_scannable_paths

    files = filter_scannable_paths(paths, repo_root=root, config=config)

Precedence and graceful degradation are documented in
``docs/refactor.md``.
"""

from __future__ import annotations

import fnmatch
import logging
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

LOG = logging.getLogger("refactor.path_filters")

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

#: Source file extensions considered in full-project scans.
SOURCE_EXTENSIONS: tuple[str, ...] = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
)

#: Directory name patterns that are never part of the project's own code.
#:
#: The first group is the pre-existing build/VCS exclusion set; the second
#: group covers vendored code and npm-installed packages (SA-0MVPDVXT90067E3M).
DEFAULT_EXCLUDED_DIRS: frozenset[str] = frozenset({
    # VCS, build output, environments, worklog state.
    ".git", "__pycache__", "build", "dist",
    ".venv", "venv", ".tox", ".eggs", "*.egg-info", ".worklog",
    # Vendored code and npm-installed packages (external by default; a
    # project can re-include any of them via ``include`` in .refactor.json).
    "node_modules", "vendor", "third_party", "third-party",
    "site-packages", "bower_components", "jspm_packages", "packages",
})

#: Git command used for batched gitignore filtering.
_GIT_CHECK_IGNORE = ["git", "check-ignore", "--no-index", "--stdin", "-z"]


# ---------------------------------------------------------------------------
# Path predicates
# ---------------------------------------------------------------------------


def has_source_extension(path: str) -> bool:
    """Return ``True`` when *path* has a scanned source extension."""
    return any(path.endswith(ext) for ext in SOURCE_EXTENSIONS)


def is_excluded_path(path: str) -> bool:
    """Return ``True`` when any component of *path* is a default exclusion.

    Matching is per path component (so exclusion works at any depth) and
    supports glob patterns such as ``*.egg-info``.
    """
    for part in _split(path):
        for pattern in DEFAULT_EXCLUDED_DIRS:
            if fnmatch.fnmatchcase(part, pattern):
                return True
    return False


def matches_patterns(path: str, patterns: Iterable[str]) -> bool:
    """Return ``True`` when *path* matches any config pattern in *patterns*.

    A pattern is matched in three ways so that both directory entries and
    file globs behave intuitively:

    - whole-path glob (``fnmatch``), e.g. ``vendor/**`` or ``*.gen.py``;
    - directory prefix (``src/legacy`` excludes ``src/legacy/old.py``);
    - component glob for slash-free patterns (``packages`` matches
      ``a/packages/b.py``; ``*.min.js`` matches any such file).
    """
    normalised = _normalise(path)
    parts = normalised.split("/")
    for raw in patterns:
        pattern = _normalise(str(raw))
        if not pattern:
            continue
        if fnmatch.fnmatchcase(normalised, pattern):
            return True
        if "/" not in pattern:
            if any(fnmatch.fnmatchcase(part, pattern) for part in parts):
                return True
        elif normalised == pattern or normalised.startswith(pattern + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def filter_scannable_paths(
    paths: Iterable[str],
    repo_root: str | Path | None = None,
    config: Any = None,
    *,
    require_source_extension: bool = True,
    use_default_excludes: bool = True,
    use_gitignore: bool = True,
) -> list[str]:
    """Return the paths that belong to this project and are worth scanning.

    Args:
        paths: Candidate file paths, relative to *repo_root*.
        repo_root: Repository root used for ``.gitignore`` resolution.
            Defaults to the current working directory.
        config: Optional configuration supplying ``exclude``/``include``
            path filters.  Accepts a
            :class:`refactor.scripts.config.RefactorConfig` or a plain
            mapping loaded from ``.refactor.json``.
        require_source_extension: Keep only source files (``True`` for
            full-project scans; session scans pass ``False`` so that
            non-source changes are still reported).
        use_default_excludes: Apply :data:`DEFAULT_EXCLUDED_DIRS`.
        use_gitignore: Apply ``.gitignore`` filtering via git.

    Returns:
        The filtered paths, preserving the input order.
    """
    excludes, includes = extract_path_filters(config)
    candidates = list(paths)

    kept: list[str] = []
    gitignore_candidates: list[str] = []

    for path in candidates:
        if not isinstance(path, str) or not path:
            continue
        normalised = _normalise(path)
        # 1. Explicit re-include wins over every exclusion, including
        #    .gitignore (the operator asked for this path).
        if matches_patterns(normalised, includes):
            kept.append(path)
            continue
        # 2. Source-extension scope (full-project scans).
        if require_source_extension and not has_source_extension(normalised):
            continue
        # 3. Default external/build directory exclusion.
        if use_default_excludes and matches_patterns(normalised, DEFAULT_EXCLUDED_DIRS):
            continue
        # 4. Project-declared excludes from .refactor.json.
        if matches_patterns(normalised, excludes):
            continue
        kept.append(path)
        if use_gitignore:
            gitignore_candidates.append(path)

    # 5. .gitignore filtering, batched into a single git call.
    if gitignore_candidates:
        ignored = _gitignored_paths(gitignore_candidates, repo_root)
        if ignored:
            LOG.info("Excluded %d gitignored path(s) from the scan", len(ignored))
            kept = [p for p in kept if p not in ignored]

    return kept


def extract_path_filters(config: Any) -> tuple[list[str], list[str]]:
    """Return ``(excludes, includes)`` declared by *config*.

    Accepts a ``RefactorConfig`` (attributes ``path_exclude`` /
    ``path_include``) or a plain mapping with ``exclude`` / ``include`` keys.
    Missing or malformed values yield empty lists, so a config without path
    keys behaves exactly like the defaults.
    """
    if config is None:
        return [], []

    if isinstance(config, Mapping):
        excludes_raw = config.get("exclude", [])
        includes_raw = config.get("include", [])
    else:
        excludes_raw = getattr(config, "path_exclude", [])
        includes_raw = getattr(config, "path_include", [])

    return _as_str_list(excludes_raw), _as_str_list(includes_raw)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _as_str_list(value: Any) -> list[str]:
    """Coerce *value* to a list of non-empty strings."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def _normalise(path: str) -> str:
    """Normalise a path/pattern for matching (posix separators, no prefix)."""
    value = path.strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value.rstrip("/") if value not in ("/",) else value


def _split(path: str) -> list[str]:
    """Split a normalised path into components."""
    return [part for part in _normalise(path).split("/") if part]


def _gitignored_paths(
    paths: list[str],
    repo_root: str | Path | None,
) -> set[str]:
    """Return the subset of *paths* ignored by ``.gitignore``.

    One batched ``git check-ignore --no-index --stdin -z`` call handles every
    candidate.  ``--no-index`` makes the check apply to tracked files too,
    which is the whole point: a tracked file can still be gitignored.

    Degrades gracefully: when git is unavailable, the directory is not a git
    repository, or the command fails, a warning is logged and an empty set is
    returned so the other filters still apply.
    """
    cwd = str(repo_root) if repo_root is not None else None
    payload = "\0".join(paths) + "\0"
    try:
        proc = subprocess.run(
            _GIT_CHECK_IGNORE,
            input=payload,
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
    except (OSError, ValueError) as exc:
        LOG.warning("git check-ignore unavailable (%s); skipping .gitignore filter", exc)
        return set()

    if proc.returncode not in (0, 1):
        LOG.warning(
            "git check-ignore failed (%d): %s; skipping .gitignore filter",
            proc.returncode,
            (proc.stderr or "").strip(),
        )
        return set()

    output = proc.stdout or ""
    if not output:
        return set()
    return {entry for entry in output.split("\0") if entry}
