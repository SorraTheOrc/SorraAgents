#!/usr/bin/env python3
"""Canonical code quality orchestrator.

Detects languages in a project, probes for available linters, runs them,
and outputs structured JSON with classified findings.

Usage:
  python3 -m skill.code_review.scripts.code_quality [--path <project-root>]
      [--languages python,typescript] [--json]

Exit codes:
  0 – success (findings may be present)
  1 – internal error
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence, Union

# Ensure the package is importable when run as __main__
_SCRIPT_DIR = Path(__file__).resolve().parent
_PACKAGE_ROOT = _SCRIPT_DIR.parent.parent.parent  # repo root
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

from skill.code_review.scripts.detection import (  # noqa: E402
    detect_languages,
    get_linters_for_language,
    probe_linter,
)
from skill.code_review.scripts.linter_runner import (  # noqa: E402
    run_linters_for_project,
)


# ---------------------------------------------------------------------------
# Language filter
# ---------------------------------------------------------------------------

SUPPORTED_LANGUAGES: set[str] = {
    "python", "typescript",
    # Phase 2 languages (detected but linters may not be fully implemented)
    "markdown", "shell", "javascript", "csharp",
}


def _normalise_language(name: str) -> str:
    """Normalise a language name to lowercase canonical form."""
    return name.strip().lower()


def _validate_languages(languages: list[str]) -> list[str]:
    """Validate and normalise a list of language names.

    Unsupported languages are filtered out with a warning to stderr.
    """
    valid: list[str] = []
    for lang in languages:
        normalised = _normalise_language(lang)
        if normalised in SUPPORTED_LANGUAGES:
            valid.append(normalised)
        else:
            print(
                f"Warning: unsupported language '{lang}' — ignoring.",
                file=sys.stderr,
            )
    return valid


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_code_quality(
    project_root: Union[str, os.PathLike[str], None] = None,
    languages: list[str] | None = None,
    runner: Any = None,
) -> dict[str, Any]:
    """Run the full code quality pipeline and return structured results.

    Args:
        project_root: Path to the project root (default: cwd).
        languages: Optional list of language names to restrict to.
                   If None, all detected languages are processed.
        runner: Optional injectable subprocess runner for testing.

    Returns:
        A dict with keys:
          - ``languages``: list of detected (or filtered) language names
          - ``linters``: list of probe results
          - ``total_findings``: total number of findings
          - ``findings_by_severity``: severity → count mapping
          - ``findings``: list of finding dicts
          - ``success``: bool — True if pipeline completed
    """
    try:
        # 1. Detect languages
        detected = detect_languages(project_root)

        # 2. Filter by requested languages (if specified)
        if languages:
            filtered = [lang for lang in detected if lang in languages]
            # Also include languages that were requested but not detected
            # (they may have linters that should be probed)
            for lang in languages:
                if lang not in filtered and lang in SUPPORTED_LANGUAGES:
                    filtered.append(lang)
            detected = filtered

        # 3. Probe linters
        linters: list[dict[str, Any]] = []
        seen: set[str] = set()
        for lang in detected:
            for linter_name in get_linters_for_language(lang):
                if linter_name not in seen:
                    seen.add(linter_name)
                    linters.append(probe_linter(linter_name))

        # 4. Run linters
        result = run_linters_for_project(project_root, runner=runner)

        # 5. Build result ensuring detected/filtered languages are reflected
        return {
            "languages": detected,
            "linters": linters,
            "total_findings": result.get("total_findings", 0),
            "findings_by_severity": result.get("findings_by_severity", {}),
            "findings": result.get("findings", []),
            "success": True,
        }
    except Exception as exc:
        return {
            "languages": [],
            "linters": [],
            "total_findings": 0,
            "findings_by_severity": {},
            "findings": [],
            "success": False,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run code quality checks on a project.",
    )
    p.add_argument(
        "--path",
        default=None,
        help="Project root directory (default: current working directory)",
    )
    p.add_argument(
        "--languages",
        default=None,
        help="Comma-separated list of languages to check "
             "(e.g. 'python,typescript'). Default: all detected languages.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output to stdout",
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Resolve languages filter
    languages: list[str] | None = None
    if args.languages:
        raw = [lang.strip() for lang in args.languages.split(",") if lang.strip()]
        languages = _validate_languages(raw)
        if not languages:
            print(
                "Error: no valid languages specified. "
                f"Supported: {', '.join(sorted(SUPPORTED_LANGUAGES))}",
                file=sys.stderr,
            )
            return 1

    try:
        result = run_code_quality(
            project_root=args.path,
            languages=languages,
        )
    except Exception as exc:
        print(f"Internal error: {exc}", file=sys.stderr)
        return 1

    if not result.get("success", False):
        error = result.get("error", "Unknown error")
        print(f"Code quality check failed: {error}", file=sys.stderr)
        return 1

    # Output
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_human_readable(result)

    return 0


def _print_human_readable(result: dict[str, Any]) -> None:
    """Print a human-readable summary of the code quality results."""
    languages = result.get("languages", [])
    linters = result.get("linters", [])
    findings = result.get("findings", [])
    severity_counts = result.get("findings_by_severity", {})

    print(f"Languages detected: {', '.join(languages) if languages else 'none'}")
    print(f"Linters probed: {len(linters)} available")
    for lint in linters:
        status = "available" if lint.get("available") else "not found"
        print(f"  - {lint['name']}: {status}")

    print(f"\nTotal findings: {len(findings)}")

    if severity_counts:
        parts = [f"{sev}: {count}" for sev, count in severity_counts.items() if count > 0]
        if parts:
            print(f"Findings by severity: {', '.join(parts)}")

    if findings:
        print("\nFindings:")
        for f in findings[:20]:  # Show first 20
            sev = f.get("severity", "?").upper()
            file_ = f.get("file", "?")
            line = f.get("line", 0)
            msg = f.get("message", "")
            code = f.get("code", "")
            print(f"  [{sev}] {file_}:{line} — {msg} ({code})")
        if len(findings) > 20:
            print(f"  ... and {len(findings) - 20} more findings")

    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    raise SystemExit(main())
