#!/usr/bin/env python3
"""Install the canonical project ``AGENTS.md`` structure.

New projects must not start by duplicating the global agent guidance into a
project-local ``AGENTS.md``. The canonical model is a single reference to the
global file (``~/.pi/agent/AGENTS.md``) plus a placeholder for the project
owner's own rules. This installer emits exactly that structure (the template
lives at ``templates/project-AGENTS.md``).

The legacy behaviour (copying the full global instruction set into the
project) remains available, but only as an **explicit opt-in** via
``--copy-global``.

Usage:
    python3 scripts/init_project_agents.py [--target DIR] [--mode append]
                                           [--copy-global] [--dry-run] [--json]

Exit codes:
    0 — success (including a deliberate no-op)
    1 — error (missing template/global source, unwritable target, ...)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Detection markers. The canonical reference line is the one emitted by the
# template; the legacy marker keeps re-runs idempotent for projects that
# adopted the earlier pointer-line model.
CANONICAL_REFERENCE_MARKER = "## Global agent guidance"
CANONICAL_REFERENCE_LINE = "Read the global agent instructions at `~/.pi/agent/AGENTS.md`"
LEGACY_REFERENCE_LINE = "Follow the global AGENTS.md in addition to the rules below."

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "templates" / "project-AGENTS.md"
GLOBAL_AGENTS_PATH = REPO_ROOT / "AGENTS_GLOBAL.md"

MODES = ("append", "overwrite", "skip")


def read_template() -> str:
    """Return the canonical template, or raise a clear error when missing."""
    try:
        return TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - defensive
        raise RuntimeError(
            f"canonical template not found at {TEMPLATE_PATH}: {exc}"
        ) from exc


def has_canonical_reference(text: str) -> bool:
    """True when the file already references the global guidance."""
    return (
        CANONICAL_REFERENCE_MARKER in text
        or CANONICAL_REFERENCE_LINE in text
        or LEGACY_REFERENCE_LINE in text
    )


def resolve_global_source(explicit: str | None) -> Path:
    """Resolve the global file to copy for the ``--copy-global`` opt-in."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(GLOBAL_AGENTS_PATH)
    candidates.append(Path.home() / ".pi" / "agent" / "AGENTS.md")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        "no global AGENTS file found to copy; pass --global-source PATH "
        f"(tried: {', '.join(str(c) for c in candidates)})"
    )


def compute_action(target: Path, template: str, mode: str) -> tuple[str, str | None]:
    """Decide what to do for ``target``.

    Returns ``(action, content)`` where ``content`` is the text to write (or
    ``None`` for a no-op / copy-global).
    """
    if not target.exists():
        return "created", template
    existing = target.read_text(encoding="utf-8")
    if has_canonical_reference(existing):
        return "noop", None
    if mode == "skip":
        return "skipped", None
    if mode == "overwrite":
        return "overwritten", template
    # append (default): put the reference above the existing project rules.
    content = template.rstrip("\n") + "\n\n" + existing.lstrip("\n")
    return "appended", content


def install(
    target_dir: Path,
    mode: str,
    copy_global: bool,
    global_source: str | None,
    dry_run: bool,
) -> dict[str, object]:
    """Perform the install and return a JSON-serialisable result."""
    target = target_dir / "AGENTS.md"
    if copy_global:
        source = resolve_global_source(global_source)
        action = "copied-global"
        content: str | None = source.read_text(encoding="utf-8")
        message = (
            "Copied the full global AGENTS content (explicit --copy-global "
            "opt-in; this duplicates the global file)."
        )
    else:
        template = read_template()
        action, content = compute_action(target, template, mode)
        message = {
            "created": "Created AGENTS.md with the canonical global reference.",
            "appended": "Prepended the canonical global reference above existing rules.",
            "overwritten": "Replaced AGENTS.md with the canonical global reference.",
            "noop": "AGENTS.md already contains the global reference; no change.",
            "skipped": "AGENTS.md exists without the reference; --mode skip left it unchanged.",
        }[action]

    if content is not None and not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    return {
        "success": True,
        "path": str(target),
        "action": action,
        "dry_run": dry_run,
        "message": message,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Emit the canonical project AGENTS.md structure.",
    )
    parser.add_argument(
        "--target",
        default=".",
        help="Project directory that receives AGENTS.md (default: current directory).",
    )
    parser.add_argument(
        "--mode",
        choices=MODES,
        default="append",
        help=(
            "What to do when AGENTS.md exists without the global reference: "
            "append (default, prepend the reference), overwrite, or skip."
        ),
    )
    parser.add_argument(
        "--copy-global",
        action="store_true",
        help=(
            "Explicit opt-in to copy the full global AGENTS content instead of "
            "the reference structure (duplicates the global file)."
        ),
    )
    parser.add_argument(
        "--global-source",
        default=None,
        help="Path to the global AGENTS file used by --copy-global.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the planned action without writing anything.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = install(
            target_dir=Path(args.target).expanduser(),
            mode=args.mode,
            copy_global=args.copy_global,
            global_source=args.global_source,
            dry_run=args.dry_run,
        )
    except (RuntimeError, OSError) as exc:
        error = {"success": False, "error": str(exc)}
        if args.json:
            print(json.dumps(error))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result))
    else:
        print(str(result["message"]))
        print(f"  {result['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
