
# <!-- REFACTOR-SA-0MVRCL0Q90016C7D
# smell: formatting
# severity: low
# description: Shebang should be at the beginning of the file
# -->

# <!-- REFACTOR-SA-0MVRBPVU1001NGDW
# smell: unknown
# severity: low
# description: Unparenthesized implicit string concatenation in collection
# -->
#!/usr/bin/env python3
"""Offline consistency check: no skill template advertises next-step producer actions.

Work-item skills end their session with the canonical report whose
``## Producer Actions`` section must carry **only** actions the producer must
take to complete the *current* stage. Routine workflow progression — ``run
/skill:<next>``, ``proceed to <next stage>``, ``review the brief, then …`` —
belongs in the Conclusion's ``Ready for <next_action>`` line, never in
Producer Actions (SA-0MVNQ3RLW006XEFW).

This script scans every ``skill/*/SKILL.md`` **final-step report template**
and fails when any line that talks about producer actions instructs or
exemplifies a routine next-step action. It is the deterministic acceptance
gate (SA-0MVQ1QK1R0052WGP) for the template wording change: zero matches
means every template conforms, a non-zero exit lists exactly the offending
templates for the change backlog.

Vocabulary
----------
The match patterns are built from the shared routine-next-step vocabulary in
``skill/shared/herdr_pane.py`` (``ROUTINE_STEP_LEADINS`` /
``ROUTINE_STEP_VERBS`` / ``ROUTINE_STEP_TARGETS``) — the same constants the
runtime ``is_routine_next_step`` classifier consumes — so the static check
cannot drift from the shared classifier (AC2).

Properties (AC3)
----------------
* Offline — reads markdown files only, no network, no ``wl``.
* Fast — a few dozen small files, pure Python.
* Deterministic — same tree always yields the same verdict.
* No new runtime dependency — stdlib + the repo's shared ``herdr_pane`` only.

Usage
-----
    python3 $(skill_path report)/scripts/check_producer_actions.py \
      [--json] [--skill-dir DIR]

Exit codes: ``0`` = no next-step producer-action instructions found (pass);
``1`` = instructions found (fail, listed); ``2`` = usage/IO error.

Examples
--------
    # Pass (current, compliant templates):
    $ python3 $(skill_path report)/scripts/check_producer_actions.py
    ✅ Consistency check passed: 17 skill templates contain no next-step
       producer-action instructions/examples.

    # Fail (a template gained an example like "--producer-actions
    # \"Proceed to /skill:plan\""):
    $ python3 $(skill_path report)/scripts/check_producer_actions.py
    ❌ Consistency check failed — next-step producer-action instruction(s)
       found in skill templates:
       implement:998: [--producer-actions "Proceed to /skill:plan"]
         (matched: "Proceed to /skill:plan")

    $ python3 $(skill_path report)/scripts/check_producer_actions.py --json
    {"success": false, "templates_scanned": 17, "skill_names": [...],
     "matches": [{"skill": "implement", "line": 998, "text": "...",
                  "phrase": "Proceed to /skill:plan"}],
     "match_count": 1}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path

#: Repository root (skill/report/scripts/ -> repo root, four parents up).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

# The report contract and the static check must stay in sync: the vocabulary
# lives in the shared herdr-pane module (single source of truth, AC2); the
# runtime classifier (is_routine_next_step, SA-0MVQ1QL5G007MLGM) consumes the
# same constants. A missing shared module is a loud failure — a check with a
# silently empty vocabulary would always pass.
from shared.herdr_pane import (
    ROUTINE_STEP_LEADINS,
    ROUTINE_STEP_TARGETS,
    ROUTINE_STEP_VERBS,
)

#: The deterministic final-step template heading every work-item skill carries.
FINAL_STEP_HEADER_RE = re.compile(
    r"^(#{1,6})\s+Final step:\s*standardized end-of-session report\s*$",
    re.IGNORECASE,
)

#: Lines that carry producer-action *content* (the flag line, section headings
#: and prose guidance). Only these are scanned, so unrelated prose inside the
#: final-step block can never trigger the check.
_PRODUCER_ACTION_LINE_RE = re.compile(r"producer[\s-]?actions?", re.IGNORECASE)


def _build_routine_next_step_re() -> re.Pattern[str]:
    """Compile the routine-next-step pattern from the shared vocabulary.

    Conservative by construction: a phrase must join a lead-in or verb from
    the shared vocabulary with a workflow target (a stage name or
    ``/skill:<name>``). Anything less specific is left unreported — prefer
    false negatives over hiding a real producer action.
    """
    lead_alt = "|".join(re.escape(item) for item in ROUTINE_STEP_LEADINS)
    verb_alt = "|".join(re.escape(item) for item in ROUTINE_STEP_VERBS)
    word_target = "|".join(re.escape(item) for item in ROUTINE_STEP_TARGETS) + r"\b"
    # Lead-in: "next step" / "then" (optionally "and then") + punctuation.
    lead_in = rf"(?:and\s+)?(?:{lead_alt})\s*[:.\-\u2013]?\s*"
    targets = rf"(?:{word_target}|/skill:[a-z][a-z0-9-]*)"
    return re.compile(
        rf"(?<![\w/])(?:"
        rf"(?:{lead_in})?(?:{verb_alt})\s+{targets}"  # "[then] run /skill:plan"
        rf"|{lead_in}{targets}"  # "next step: implement"
        rf")",
        re.IGNORECASE,
    )


ROUTINE_NEXT_STEP_RE = _build_routine_next_step_re()


@dataclass(frozen=True)
class Match:
    """One next-step producer-action instruction found in a template."""

    skill: str
    line: int
    text: str
    phrase: str

    def to_dict(self) -> dict:
        return asdict(self)


def extract_final_step_block(markdown: str) -> tuple[int, list[str]] | None:
    """Return ``(start_line, block_lines)`` of the final-step report template.

    The block starts at the ``Final step: standardized end-of-session report``
    heading (any heading level) and ends before the next heading of the same
    or a higher level. Skills without the heading return ``None`` (they do not
    carry the report template and are out of scope).
    """
    lines = markdown.splitlines()
    for index, raw in enumerate(lines):
        match = FINAL_STEP_HEADER_RE.match(raw.rstrip())
        if not match:
            continue
        level = len(match.group(1))
        end = len(lines)
        for j in range(index + 1, len(lines)):
            heading = re.match(r"^(#{1,6})\s+\S", lines[j])
            if heading and len(heading.group(1)) <= level:
                end = j
                break
        return index + 1, lines[index:end]
    return None


def scan_block(block_lines: list[str], start_line: int = 1) -> list[Match]:
    """Return next-step producer-action matches inside a final-step block.

    Only lines that mention producer actions are examined (the
    ``--producer-actions`` flag line, ``## Producer Actions`` headings, prose
    guidance), so unrelated sentences in the block cannot trip the check.
    Each match's ``skill`` field is left empty — file-level callers fill it in.
    """
    matches: list[Match] = []
    for offset, line in enumerate(block_lines):
        if not _PRODUCER_ACTION_LINE_RE.search(line):
            continue
        phrase_match = ROUTINE_NEXT_STEP_RE.search(line)
        if phrase_match:
            matches.append(
                Match(
                    skill="",
                    line=start_line + offset,
                    text=line.strip(),
                    phrase=phrase_match.group(0).strip(),
                )
            )
    return matches


def scan_skill_file(path: Path) -> tuple[str | None, list[Match]]:
    """Scan one ``skill/<name>/SKILL.md``; return ``(name, matches)``.

    ``name`` is ``None`` when the skill does not carry the report template.
    """
    text = path.read_text(encoding="utf-8")
    block = extract_final_step_block(text)
    if block is None:
        return None, []
    start_line, block_lines = block
    return path.parent.name, scan_block(block_lines, start_line)


def scan_skill_dir(skill_dir: Path) -> tuple[list[str], list[Match]]:
    """Scan every ``skill/*/SKILL.md`` under *skill_dir*.

    Returns ``(template_skills, matches)`` where *template_skills* is the
    sorted list of skill names whose SKILL.md carries the report template.
    """
    template_skills: list[str] = []
    matches: list[Match] = []
    for path in sorted(skill_dir.glob("*/SKILL.md")):
        name, found = scan_skill_file(path)
        if name is None:
            continue
        template_skills.append(name)
        for match in found:
            matches.append(replace(match, skill=name))
    return template_skills, matches


def _render_text(template_skills: list[str], matches: list[Match]) -> str:
    count = len(template_skills)
    if not matches:
        return (
            f"✅ Consistency check passed: {count} skill templates contain no "
            "next-step producer-action instructions/examples."
        )
    lines = [
        f"❌ Consistency check failed — next-step producer-action "
        f"instruction(s) found in {len(matches)} skill template(s):"
    ]
    for match in matches:
        lines.append(
            f"  {match.skill}:{match.line}: {match.text} "
            f"(matched: {match.phrase!r})"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Consistency check: no skill/*/SKILL.md final-step report template "
            "instructs or exemplifies next-step producer actions."
        )
    )
    parser.add_argument(
        "--skill-dir",
        type=Path,
        default=REPO_ROOT / "skill",
        help="Directory containing skill/*/SKILL.md (default: <repo>/skill).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit a JSON report instead of prose."
    )
    args = parser.parse_args(argv)

    try:
        template_skills, matches = scan_skill_dir(args.skill_dir)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "success": not matches,
                    "templates_scanned": len(template_skills),
                    "skill_names": template_skills,
                    "matches": [m.to_dict() for m in matches],
                    "match_count": len(matches),
                },
                indent=2,
            )
        )
    else:
        print(_render_text(template_skills, matches))
    return 0 if not matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
