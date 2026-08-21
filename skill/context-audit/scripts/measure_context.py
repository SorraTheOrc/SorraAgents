#!/usr/bin/env python3
"""Measure the pi session startup static-context surface.

Reports bytes and a rough token estimate (chars/4) for each startup-context
component of a pi session:

- ``global_agents``  — ``AGENTS_GLOBAL.md`` at the repo root
- ``project_agents`` — project ``AGENTS.md`` at the repo root
- ``skills_prose``   — sum of the frontmatter ``description`` prose across
  ``skill/*/SKILL.md`` files (the skills-discovery section of the prompt)
- ``total``          — sum of the above

Exits with a machine-readable summary (JSON or key=value). When thresholds are
provided, exits non-zero when any measured byte count exceeds its threshold
(regression gate usable in CI).

Usage:
    python3 skill/context-audit/scripts/measure_context.py
    python3 skill/context-audit/scripts/measure_context.py --json
    python3 skill/context-audit/scripts/measure_context.py --keyvalue
    python3 skill/context-audit/scripts/measure_context.py \\
        --thresholds docs/dev/context-budget.thresholds.json
    python3 skill/context-audit/scripts/measure_context.py --threshold total=26000
    python3 skill/context-audit/scripts/measure_context.py --generate-thresholds
    python3 skill/context-audit/scripts/measure_context.py \
        --write-thresholds docs/dev/context-budget.thresholds.json

Exit codes:
    0  measured within thresholds (or no thresholds given)
    1  usage/measurement error
    2  one or more thresholds exceeded
"""  # noqa: EXE001

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Rough token estimate: chars / 4 (per the context-budget baseline).
CHARS_PER_TOKEN = 4
GLOBAL_AGENTS_FILENAME = "AGENTS_GLOBAL.md"
PROJECT_AGENTS_FILENAME = "AGENTS.md"
SKILL_MD_GLOB = "skill/*/SKILL.md"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_THRESHOLD_EXCEEDED = 2


# ── Parsing helpers ────────────────────────────────────────────────────────


def extract_frontmatter(text: str) -> str:
    """Return the YAML frontmatter block (between leading ``---`` markers).

    Returns an empty string when the text has no frontmatter.
    """
    match = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", text, re.DOTALL)
    return match.group(1) if match else ""


def parse_description(frontmatter: str) -> str:
    """Extract the ``description`` value from a YAML-ish frontmatter block.

    Supports single-line quoted/unquoted scalars and literal block scalars
    (``description: |``). Returns an empty string when absent.
    """
    lines = frontmatter.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("description:"):
            continue
        rest = line[len("description:"):].strip()
        if not rest:
            # Indented plain scalar: collect indented continuation lines.
            block_lines: list[str] = []
            for sub in lines[index + 1:]:
                if sub == "" or sub.startswith(("  ", "\t")):
                    block_lines.append(sub.removeprefix("  ") if sub.startswith("  ") else sub)
                else:
                    break
            return "\n".join(block_lines).strip()
        if rest.startswith(("|", ">")):
            block_lines: list[str] = []
            for sub in lines[index + 1:]:
                if sub == "" or sub.startswith(("  ", "\t")):
                    block_lines.append(sub.removeprefix("  ") if sub.startswith("  ") else sub)
                else:
                    break
            return "\n".join(block_lines).strip()
        if rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
            return rest[1:-1].replace('\\"', '"')
        return rest
    return ""


def estimate_tokens(chars: int) -> int:
    """Rough token estimate for a character count (chars / 4)."""
    if chars <= 0:
        return 0
    return max(1, round(chars / CHARS_PER_TOKEN))


# ── Measurement ────────────────────────────────────────────────────────────


def file_bytes(path: Path) -> int:
    """Byte count of a UTF-8 text file, or 0 when the file is missing."""
    try:
        return len(path.read_text(encoding="utf-8").encode("utf-8"))
    except FileNotFoundError:
        return 0


def skill_description_prose(
    repo_root: Path, include_hidden: bool = False
) -> dict[str, str]:
    """Map each ``skill/<name>/SKILL.md`` to its frontmatter description.

    Only directories containing a ``SKILL.md`` are counted (internal script
    dirs such as ``skill/scripts/`` are excluded automatically). Skills with
    ``disable-model-invocation: true`` are excluded by default because they
    do not appear in the session's skills-discovery block; pass
    ``include_hidden=True`` to audit all skills.
    """
    result: dict[str, str] = {}
    for skill_md in sorted(repo_root.glob(SKILL_MD_GLOB)):
        text = skill_md.read_text(encoding="utf-8")
        frontmatter = extract_frontmatter(text)
        if not include_hidden and "disable-model-invocation: true" in frontmatter:
            continue
        result[skill_md.parent.name] = parse_description(frontmatter)
    return result


def hidden_skill_names(repo_root: Path) -> list[str]:
    """Names of skills with ``disable-model-invocation: true``."""
    names: list[str] = []
    for skill_md in sorted(repo_root.glob(SKILL_MD_GLOB)):
        frontmatter = extract_frontmatter(skill_md.read_text(encoding="utf-8"))
        if "disable-model-invocation: true" in frontmatter:
            names.append(skill_md.parent.name)
    return names


def _component(chars: int) -> dict:
    """Build a per-component measurement dict from a character count."""
    return {"chars": chars, "bytes": chars, "tokens": estimate_tokens(chars)}


def measure(repo_root: Path, include_hidden: bool = False) -> dict:
    """Measure the startup-context surface for a repo root.

    Args:
        repo_root: Repository root to measure.
        include_hidden: Include ``disable-model-invocation`` skills in the
            prose count (default False matches the session startup surface,
            which excludes hidden skills).

    Returns a dict of component name → measurement, plus ``total``.
    """
    global_chars = file_bytes(repo_root / GLOBAL_AGENTS_FILENAME)
    project_chars = file_bytes(repo_root / PROJECT_AGENTS_FILENAME)
    prose = skill_description_prose(repo_root, include_hidden=include_hidden)
    prose_chars = sum(len(desc) for desc in prose.values())

    components: dict[str, dict] = {
        "global_agents": _component(global_chars),
        "project_agents": _component(project_chars),
        "skills_prose": {
            **_component(prose_chars),
            "skill_count": len(prose),
            "hidden_skill_count": len(hidden_skill_names(repo_root)),
        },
    }
    total_chars = global_chars + project_chars + prose_chars
    components["total"] = _component(total_chars)
    return components


def load_thresholds(args) -> dict[str, int]:
    """Load threshold overrides from ``--thresholds`` file and ``--threshold`` args."""
    thresholds: dict[str, int] = {}
    if args.thresholds_file:
        try:
            data = json.loads(
                Path(args.thresholds_file).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise argparse.ArgumentTypeError(
                f"cannot read thresholds file {args.thresholds_file}: {exc}"
            ) from exc
        thresholds.update(
            {str(k): int(v) for k, v in data.items()}
        )
    for spec in args.threshold or []:
        if "=" not in spec:
            raise argparse.ArgumentTypeError(
                f"invalid threshold spec {spec!r}; expected NAME=BYTES"
            )
        name, _, value = spec.partition("=")
        thresholds[name.strip()] = int(value.strip())
    return thresholds


# ── Output ─────────────────────────────────────────────────────────────────


def format_text(components: dict, thresholds: dict) -> str:
    """Human-readable table."""
    lines = ["Startup static-context budget (bytes / tokens):"]
    for name, comp in components.items():
        if name == "total":
            continue
        label = f"{name:<15}"
        extra = (
            f" ({comp['skill_count']} skills)" if "skill_count" in comp else ""
        )
        lines.append(f"  {label} : {comp['bytes']:>6} B / {comp['tokens']:>5} tok{extra}")
    total = components["total"]
    lines.append(f"  {'total':<15} : {total['bytes']:>6} B / {total['tokens']:>5} tok")
    if thresholds:
        lines.append("Thresholds: " + ", ".join(
            f"{k}={v}" for k, v in sorted(thresholds.items())
        ))
    return "\n".join(lines)


def format_keyvalue(components: dict) -> str:
    """Machine-readable ``component.field=value`` lines."""
    lines = []
    for name, comp in components.items():
        for field in ("bytes", "tokens"):
            lines.append(f"{name}.{field}={comp[field]}")
        if "skill_count" in comp:
            lines.append(f"{name}.skill_count={comp['skill_count']}")
    return "\n".join(lines)


def thresholds_from_components(components: dict) -> dict[str, int]:
    """Build a thresholds dict from a measurement's per-component byte counts.

    Mirrors the committed thresholds-file format used by the regression gate:
    ``{component: max_bytes, ...}`` for the four components global_agents,
    project_agents, skills_prose, total.
    """
    return {
        name: comp["bytes"]
        for name, comp in components.items()
    }


def format_json(repo_root: Path, components: dict, thresholds: dict,
                exceeded: list[str]) -> str:
    """Machine-readable JSON summary."""
    return json.dumps(
        {
            "repo_root": str(repo_root),
            "components": components,
            "thresholds": thresholds or None,
            "threshold_exceeded": exceeded,
        },
        indent=2,
        sort_keys=True,
    )


# ── CLI ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure the pi session startup static-context surface.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Repository root to measure (default: repo containing this script).",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON summary.",
    )
    output.add_argument(
        "--keyvalue",
        action="store_true",
        help="Emit machine-readable key=value lines.",
    )
    output.add_argument(
        "--generate-thresholds",
        action="store_true",
        help="Measure the repo and emit a thresholds JSON object to stdout "
        "(per-component measured byte values for the regression gate).",
    )
    parser.add_argument(
        "--write-thresholds",
        dest="thresholds_out",
        metavar="FILE",
        help="Measure the repo and write a thresholds JSON file at FILE "
        "(per-component measured byte values for the regression gate).",
    )
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="Count disable-model-invocation skills in skills prose "
        "(default excludes them, matching the session startup surface).",
    )
    parser.add_argument(
        "--thresholds",
        dest="thresholds_file",
        metavar="FILE",
        help="JSON file mapping component name to max bytes (regression gate).",
    )
    parser.add_argument(
        "--threshold",
        dest="threshold",
        action="append",
        metavar="NAME=BYTES",
        help="Inline threshold, e.g. --threshold total=26000 (repeatable).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the measurement; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    repo_root: Path = args.repo_root.resolve()

    if not repo_root.is_dir():
        print(f"Error: repo root not found: {repo_root}", file=sys.stderr)
        return EXIT_ERROR

    try:
        thresholds = load_thresholds(args)
    except (argparse.ArgumentTypeError, ValueError) as exc:
        parser.error(str(exc))
        return EXIT_ERROR  # pragma: no cover (argparse exits)

    components = measure(repo_root, include_hidden=args.include_hidden)
    exceeded = sorted(
        name for name, limit in thresholds.items()
        if components.get(name, {}).get("bytes", 0) > limit
    )

    if args.generate_thresholds or args.thresholds_out:
        threshold_data = thresholds_from_components(components)
        payload = json.dumps(threshold_data, indent=2, sort_keys=True)
        if args.thresholds_out:
            try:
                Path(args.thresholds_out).write_text(payload + "\n", encoding="utf-8")
            except OSError as exc:
                print(f"Error: cannot write thresholds file {args.thresholds_out}: {exc}",
                      file=sys.stderr)
                return EXIT_ERROR
        else:
            print(payload)
    elif args.json:
        print(format_json(repo_root, components, thresholds, exceeded))
    elif args.keyvalue:
        print(format_keyvalue(components))
    else:
        print(format_text(components, thresholds))

    if exceeded:
        print(
            "Thresholds exceeded: "
            + ", ".join(
                f"{name} ({components[name]['bytes']} B > {thresholds[name]} B)"
                for name in exceeded
            ),
            file=sys.stderr,
        )
        return EXIT_THRESHOLD_EXCEEDED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
