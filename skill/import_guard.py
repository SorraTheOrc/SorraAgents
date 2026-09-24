"""Canonical graceful-failure guard for skill shared-import blocks (F3, SA-0MSWJ9ZEU001HDVT).

When a skill script cannot resolve its required shared modules (e.g. a
partial or copied installation where ``shared`` is missing at the skills
root), it must fail GRACEFULLY with an actionable, non-destructive error.
This module lives at the skills root itself (sibling of ``shared/``,
``scripts/``, ``audit/``, ...) so it is importable as ``import_guard`` by
any skill script once its skills-root bootstrap has run.

The error message:

- names the missing module,
- gives the canonical invocation (the global skills install /
  ``$(skill_path <name>)``),
- states the NO cross-repo-copy rule, and
- never suggests or performs file copies between repositories.
"""

from __future__ import annotations

import sys


def guard_shared_import(module: str, script: str | None = None) -> None:
    """Print the canonical missing-shared error and exit 1.

    Args:
        module: The dotted module name that could not be imported (usually
            the ``ModuleNotFoundError.name`` attribute).
        script: Optional script name/path for a more actionable hint; when
            omitted the message refers to the skill generally.
    """
    where = f" (via {script})" if script else ""
    print(
        f"ERROR: required shared module '{module}' could not be imported{where}.\n"
        "This skill expects its shared libraries to be installed at the canonical\n"
        "global skills location: ~/.pi/agent/skills/ (resolve the current skill with\n"
        "`$(skill_path <skill-name>)`).\n"
        "Do NOT copy skill scripts between repositories — never paste script\n"
        "files into other projects. Instead invoke the skill from its\n"
        "canonical global location, or reinstall via scripts/install_pi.sh.",
        file=sys.stderr,
    )
    sys.exit(1)