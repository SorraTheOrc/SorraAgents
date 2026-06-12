"""Language detection and linter probing for code quality automation.

Provides:
  - detect_languages(): Scan a project directory for known file extensions
    and return a list of detected language names.
  - probe_linter(): Check if a linter tool is available on PATH and return
    structured availability information.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable, Union

# ---------------------------------------------------------------------------
# Language-to-extension mapping
# ---------------------------------------------------------------------------
# Each language maps to a set of file extensions that identify it.
# Extensions are lowercase and compared case-insensitively.

LANGUAGE_EXTENSIONS: dict[str, set[str]] = {
    "python": {".py", ".pyi", ".pyx"},
    "typescript": {".ts", ".tsx"},
    "markdown": {".md", ".markdown"},
    "shell": {".sh", ".bash", ".zsh", ".ksh"},
    "javascript": {".js", ".mjs", ".cjs", ".jsx"},
    "csharp": {".cs", ".csproj"},
}

# Linters we can probe for each language.
LANGUAGE_LINTERS: dict[str, list[str]] = {
    "python": ["ruff"],
    "typescript": ["eslint"],
    "markdown": ["markdownlint"],
    "shell": ["shellcheck"],
    "javascript": ["eslint"],
    "csharp": ["dotnet-format"],
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_languages(
    project_root: Union[str, os.PathLike[str], None] = None,
) -> list[str]:
    """Detect programming languages present in *project_root*.

    Scans the directory tree (recursively) for files with recognised
    extensions and returns a sorted list of language names detected.

    Hidden directories (names starting with ``.``) are skipped.

    Also detects Node.js projects via ``package.json`` presence.

    Args:
        project_root: Path to the project root directory.
                      If None or not provided, defaults to the current
                      working directory.

    Returns:
        A sorted list of language names (e.g. ``["python", "typescript"]``).
        Returns an empty list if the directory does not exist or no
        recognised files are found.
    """
    if project_root is None:
        root = Path.cwd()
    elif isinstance(project_root, os.PathLike):
        root = Path(project_root)
    else:
        root = Path(str(project_root))

    if not root.is_dir():
        return []

    detected: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden directories
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        for filename in filenames:
            ext = os.path.splitext(filename)[1].lower()
            for language, extensions in LANGUAGE_EXTENSIONS.items():
                if ext in extensions:
                    detected.add(language)

    # Detect Node.js via package.json in the root directory
    if (root / "package.json").exists():
        detected.add("javascript")

    return sorted(detected)


def probe_linter(linter_name: str) -> dict[str, object]:
    """Check whether a linter tool is available on the system PATH.

    Args:
        linter_name: The name of the linter executable (e.g. ``"ruff"``,
                     ``"eslint"``).

    Returns:
        A dict with keys ``name`` (str) and ``available`` (bool).
        Example: ``{"name": "ruff", "available": True}``
    """
    available = shutil.which(linter_name) is not None
    return {"name": linter_name, "available": available}


def get_linters_for_language(language: str) -> list[str]:
    """Return the list of linter names recommended for a given language.

    Args:
        language: A language name returned by :func:`detect_languages`.

    Returns:
        A list of linter executable names. Returns an empty list for
        unknown languages.
    """
    return list(LANGUAGE_LINTERS.get(language, []))


def get_linters_for_project(
    project_root: Union[str, os.PathLike[str], None] = None,
) -> list[dict[str, object]]:
    """Convenience: detect languages and probe linters in one call.

    Args:
        project_root: Path to the project root (same semantics as
                      :func:`detect_languages`).

    Returns:
        A list of probe results — one dict per linter per detected
        language. Each dict has ``name`` and ``available`` keys as
        returned by :func:`probe_linter`.
    """
    languages = detect_languages(project_root)
    linters: list[dict[str, object]] = []
    for lang in languages:
        for linter_name in get_linters_for_language(lang):
            result = probe_linter(linter_name)
            if result not in linters:
                linters.append(result)
    return linters


def get_full_report(
    project_root: Union[str, os.PathLike[str], None] = None,
) -> dict[str, object]:
    """Return a complete structured report combining detection and probing.

    Args:
        project_root: Path to the project root.

    Returns:
        A dict with keys:
          - ``languages``: list of detected language names
          - ``linters``: list of probe results (each with ``name``,
            ``available``)
        Suitable for JSON serialization.
    """
    languages = detect_languages(project_root)
    linters: list[dict[str, object]] = []
    seen: set[str] = set()
    for lang in languages:
        for linter_name in get_linters_for_language(lang):
            if linter_name not in seen:
                seen.add(linter_name)
                linters.append(probe_linter(linter_name))
    return {"languages": languages, "linters": linters}
