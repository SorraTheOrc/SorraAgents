"""Project-local extensions for global skills (SA-0MSQ7MQEJ0064ZB0).

A global skill shipped by this repository is static. An invoking project can
augment it *without forking or editing the global copy* by creating a
directory under its own repo root::

    <project_root>/.pi/skills_extensions/<skill-name>/

containing any of the following optional files:

``SKILL_PREFIX.md``
    Prose guidance surfaced to the agent **before** the skill's first
    actionable step.
``SKILL_POSTFIX.md``
    Prose guidance surfaced to the agent **after** the skill's final step.
``extension.json``
    A machine-readable JSON object consumed directly by the skill's scripts
    (e.g. the test skill's ``types`` map, SA-0MTJQB2MA008HMO6). The schema is
    owned by the consuming skill; this loader only guarantees that the file
    parses to a JSON object.

Design rules (see ``docs/dev/skill-extensions.md``):

- **Repo-root scoped discovery.** The project root is the invoking git root
  (``git rev-parse --show-toplevel``), falling back to the invoking cwd when
  git is unavailable. There is exactly one discovery path — this convention.
- **Absence is a no-op.** A missing extension directory or missing files are
  never an error and never change global skill behaviour.
- **Malformed data is loud.** A present-but-unparseable ``extension.json``
  (or an unreadable file) raises :class:`SkillExtensionError` naming the file
  — never a silent fallback.
- **Additive only.** Extensions cannot disable or weaken a skill's safety or
  gating steps; they only augment it.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DATA_FILENAME",
    "EXTENSIONS_DIR",
    "POSTFIX_FILENAME",
    "PREFIX_FILENAME",
    "SkillExtension",
    "SkillExtensionError",
    "extension_dir",
    "load_extension",
    "resolve_project_root",
]

#: Directory (relative to the project root) holding per-skill extensions.
EXTENSIONS_DIR = ".pi/skills_extensions"
#: Optional prose file injected before the skill's first actionable step.
PREFIX_FILENAME = "SKILL_PREFIX.md"
#: Optional prose file injected after the skill's final step.
POSTFIX_FILENAME = "SKILL_POSTFIX.md"
#: Optional machine-readable data file consumed by skill scripts.
DATA_FILENAME = "extension.json"

# Skill names are single path components — never allow traversal.
_ALLOWED_SKILL_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")


class SkillExtensionError(RuntimeError):
    """Raised when a present extension file cannot be read or is malformed.

    The message always names the offending file so the diagnostic is
    actionable (AC3).
    """


@dataclass(frozen=True)
class SkillExtension:
    """The resolved extension for one skill in one project.

    ``prefix``/``postfix`` are the file contents (``None`` when the file is
    absent; an empty file yields ``""``). ``data`` is the parsed
    ``extension.json`` object (``{}`` when absent or empty). ``present`` is
    True when the project defines any extension component for the skill.
    """

    skill_name: str
    project_root: Path
    directory: Path
    prefix: str | None = None
    postfix: str | None = None
    data: Mapping[str, Any] | None = None
    prefix_path: Path | None = None
    postfix_path: Path | None = None
    data_path: Path | None = None

    @property
    def present(self) -> bool:
        """True when any extension component exists for this skill."""
        return self.prefix is not None or self.postfix is not None or self.data_path is not None


def _validate_skill_name(skill_name: str) -> str:
    """Validate that *skill_name* is a single, safe path component."""
    if not skill_name or not set(skill_name).issubset(_ALLOWED_SKILL_CHARS):
        raise ValueError(
            f"Invalid skill name {skill_name!r}: expected a single path component "
            "containing only letters, digits, '-', '_' or '.'."
        )
    if skill_name in (".", "..") or ".." in skill_name:
        raise ValueError(
            f"Invalid skill name {skill_name!r}: path traversal is not allowed."
        )
    return skill_name


def resolve_project_root(start: str | Path | None = None) -> Path:
    """Resolve the invoking project's root directory.

    Uses ``git rev-parse --show-toplevel`` run from *start* (default: the
    current working directory), so the extension is discovered from the repo
    containing the working directory — including inside a git worktree.
    Falls back to *start* itself when git is unavailable or *start* is not in
    a git repository (documented non-git fallback).

    Args:
        start: Directory (or file) to resolve from. Defaults to cwd.

    Returns:
        The absolute project-root path.
    """
    start_path = Path(start or os.getcwd())
    if start_path.is_file():
        start_path = start_path.parent
    start_path = start_path.resolve()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start_path),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return start_path
    if proc.returncode == 0:
        root = Path(proc.stdout.strip())
        if root.is_dir():
            return root
    return start_path


def extension_dir(
    skill_name: str,
    project_root: str | Path | None = None,
) -> Path:
    """Return the extension directory for *skill_name* in *project_root*.

    The directory need not exist — callers should use
    :func:`load_extension` to read its contents and check
    :attr:`SkillExtension.present`.
    """
    _validate_skill_name(skill_name)
    root = (
        Path(project_root).resolve()
        if project_root is not None
        else resolve_project_root()
    )
    return root / EXTENSIONS_DIR / skill_name


def _read_optional_text(path: Path) -> str | None:
    """Read *path* as UTF-8 text, or ``None`` when it does not exist.

    Raises:
        SkillExtensionError: when the path exists but cannot be read (e.g. it
            is a directory or has invalid encoding) — malformed input must be
            loud rather than silently ignored.
    """
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillExtensionError(
            f"Unable to read skill extension file {path}: {exc}"
        ) from exc


def load_extension(
    skill_name: str,
    project_root: str | Path | None = None,
) -> SkillExtension:
    """Load the project-local extension for *skill_name*.

    Args:
        skill_name: The global skill name (e.g. ``"test"``).
        project_root: Explicit project root; when omitted it is resolved via
            :func:`resolve_project_root`.

    Returns:
        A :class:`SkillExtension`. A missing directory yields an object with
        ``present == False`` and all components ``None``/empty — never an
        error.

    Raises:
        ValueError: when *skill_name* is not a safe single path component.
        SkillExtensionError: when a present ``extension.json`` is unparseable,
            is not a JSON object, or a present file cannot be read.
    """
    _validate_skill_name(skill_name)
    root = (
        Path(project_root).resolve()
        if project_root is not None
        else resolve_project_root()
    )
    directory = root / EXTENSIONS_DIR / skill_name

    prefix_path = directory / PREFIX_FILENAME
    postfix_path = directory / POSTFIX_FILENAME
    data_path = directory / DATA_FILENAME

    prefix = _read_optional_text(prefix_path)
    postfix = _read_optional_text(postfix_path)

    data: dict[str, Any] = {}
    resolved_data_path: Path | None = None
    if data_path.exists():
        if not data_path.is_file():
            raise SkillExtensionError(
                f"Skill extension data path {data_path} exists but is not a file."
            )
        try:
            raw = data_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SkillExtensionError(
                f"Unable to read skill extension file {data_path}: {exc}"
            ) from exc
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise SkillExtensionError(
                f"Malformed JSON in skill extension file {data_path}: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise SkillExtensionError(
                f"Invalid skill extension file {data_path}: expected a JSON "
                f"object at the top level, got {type(parsed).__name__}."
            )
        data = parsed
        resolved_data_path = data_path

    return SkillExtension(
        skill_name=skill_name,
        project_root=root,
        directory=directory,
        prefix=prefix,
        postfix=postfix,
        data=data,
        prefix_path=prefix_path if prefix is not None else None,
        postfix_path=postfix_path if postfix is not None else None,
        data_path=resolved_data_path,
    )
