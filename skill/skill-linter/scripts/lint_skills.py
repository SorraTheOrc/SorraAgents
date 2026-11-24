"""Deterministic detector core for the skill-linter skill.

Work item: SA-0MWGAYPMX002Z0VC (child of SA-0MWFP1E9P0027EOE). This module is
the deterministic half of the linter: regex + structural comparison only, no
LLM dependency. The CLI (report generator, ``--apply``, ``--install-cron``)
wraps these primitives and is implemented by sibling children.

Two finding families are detected:

1. **Duplicate content** — identical blocks of consecutive non-blank lines
   repeated within one document or across documents. Exact duplicates are
   ``FIX_SAFE`` (they can only be removed by deleting a copy). Near-identical
   blocks (normalised similarity at or above ``NEAR_DUPLICATE_RATIO`` but not
   identical) are ``FIX_RECOMMENDATION``: dropping one needs operator
   judgement.
2. **Traceability noise** — non-essential work-item IDs (``SA-0…`` pattern)
   and workflow-comment lines (session logging, dispatch, claim release,
   status chatter emitted by the agent workflow). Both are ``FIX_SAFE``:
   removing them cannot lose an instruction.

Fix classes (parent AC2/AC3 permission gating):

- ``FIX_SAFE`` — deterministic, context-free removal; ``--apply`` may delete
  it without operator approval.
- ``FIX_RECOMMENDATION`` — needs operator judgement; reported with full
  context and never applied automatically.

Heuristic rules pinned by ``skill/skill-linter/tests/test_lint_skills.py`` and
mirrored in ``docs/dev/skill-linter-reference.md``:

- A duplicate window is at least ``MIN_DUPLICATE_LINES`` consecutive non-blank
  lines and at least ``MIN_DUPLICATE_CHARS`` characters long.
- Lines are compared after stripping leading/trailing whitespace; reported
  spans and text use the original (unmodified) lines.
- A work-item ID matches ``WORK_ITEM_ID_RE`` (``<PREFIX>-<digit>…``, e.g.
  ``SA-0MWGAYPMX002Z0VC``). IDs in the caller's allow-list are essential and
  preserved; every other ID is non-essential traceability noise.
- Workflow-comment lines are matched whole-line by ``WORKFLOW_COMMENT_RULES``;
  an instruction that merely *mentions* a session id is not noise.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher

#: Fix classes — the permission-gating taxonomy (parent AC3).
FIX_SAFE = "safe"
FIX_RECOMMENDATION = "recommendation"

#: Finding kinds.
KIND_DUPLICATE = "duplicate"
KIND_NEAR_DUPLICATE = "near_duplicate"
KIND_NONESSENTIAL_ID = "nonessential_id"
KIND_WORKFLOW_COMMENT = "workflow_comment"

#: Duplicate-detection floor: consecutive non-blank lines and characters.
MIN_DUPLICATE_LINES = 3
MIN_DUPLICATE_CHARS = 80

#: Similarity at or above which a non-identical block is a near-duplicate.
NEAR_DUPLICATE_RATIO = 0.85

#: Work-item IDs: an uppercase prefix, a hyphen, then a digit and ASCII
#: alphanumerics (e.g. ``SA-0MWGAYPMX002Z0VC``, ``WL-0MSS55LFU00973S2``).
WORK_ITEM_ID_RE = re.compile(r"\b[A-Z]{2,6}-\d[0-9A-Z]{8,}\b")

#: Whole-line workflow-noise rules, paired with a human-readable label used in
#: the finding rationale. Order matters only for labelling.
WORKFLOW_COMMENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        # ``implement - Session ID: 01a2…-… - /path/session.jsonl``. The id
        # must be a real session id (UUID or herdr id), so an instruction that
        # documents the placeholder (``Session ID: <pi_session_id>``) is not
        # mistaken for noise.
        "session-logging line",
        re.compile(
            r"^\s*.*?Session ID:\s*"
            r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
            r"|herdr-\d+(?:-\d+)+)"
            r"\s+-\s+\S+"
        ),
    ),
    ("dispatch line", re.compile(r"^\s*Auto-dispatched by\b", re.IGNORECASE)),
    ("claim-release line", re.compile(r"^\s*Claim released:")),
    (
        "implement status line",
        re.compile(r"^\s*implement (?:started|complete)\b", re.IGNORECASE),
    ),
    ("phase status line", re.compile(r"^\s*(?:Start|Finish) phase:")),
    ("completion marker", re.compile(r"^\s*Work committed to dev\s*$")),
)


@dataclass(frozen=True)
class Span:
    """One instance of a finding: where it lives and the original text."""

    path: str
    start_line: int
    end_line: int
    text: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "text": self.text,
        }


@dataclass(frozen=True)
class Finding:
    """A single detectable issue with its fix class and full context."""

    kind: str
    fix_class: str
    text: str
    spans: tuple[Span, ...]
    rationale: str

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "fix_class": self.fix_class,
            "text": self.text,
            "spans": [span.to_dict() for span in self.spans],
            "rationale": self.rationale,
        }


def _lines_by_path(documents: Mapping[str, str]) -> dict[str, list[str]]:
    """Normalise the document mapping to ``{path: lines}``."""
    return {str(path): text.splitlines() for path, text in documents.items()}


def _window_extension(
    docs: Mapping[str, list[str]],
    occurrences: list[tuple[str, int]],
    min_lines: int,
) -> int:
    """Longest run length shared by every occurrence, starting at ``min_lines``."""
    length = min_lines
    anchor_path, anchor_start = occurrences[0]
    while True:
        index = anchor_start + length
        if index >= len(docs[anchor_path]):
            return length
        extension = docs[anchor_path][index].strip()
        if not extension:
            return length
        for path, start in occurrences[1:]:
            other = start + length
            if other >= len(docs[path]) or docs[path][other].strip() != extension:
                return length
        length += 1


def find_duplicates(
    documents: Mapping[str, str],
    *,
    min_lines: int = MIN_DUPLICATE_LINES,
    min_chars: int = MIN_DUPLICATE_CHARS,
    include_near: bool = False,
    near_ratio: float = NEAR_DUPLICATE_RATIO,
) -> list[Finding]:
    """Report duplicated blocks within and across ``documents``.

    Args:
        documents: Mapping of file path → file text.
        min_lines: Minimum consecutive non-blank lines in a duplicated block.
        min_chars: Minimum characters in the duplicated text.
        include_near: Also report near-duplicates (recommendation class).
        near_ratio: Similarity threshold for near-duplicates.

    Returns:
        Findings sorted by first span ``(path, start_line)``. Each exact
        duplicate is reported once with every instance span.
    """
    if min_lines < 2:
        raise ValueError("min_lines must be at least 2")

    docs = _lines_by_path(documents)

    windows: dict[tuple[str, ...], list[tuple[str, int]]] = {}
    for path in sorted(docs):
        lines = docs[path]
        for start in range(len(lines) - min_lines + 1):
            window = lines[start : start + min_lines]
            if any(not line.strip() for line in window):
                continue
            key = tuple(line.strip() for line in window)
            windows.setdefault(key, []).append((path, start))

    # Claim the longest runs first so a short prefix cannot mask the longer
    # duplicate that contains it.
    candidates = []
    for key, occurrences in windows.items():
        if len(occurrences) < 2:
            continue
        occurrences = sorted(set(occurrences))
        extension = _window_extension(docs, occurrences, min_lines)
        candidates.append((-extension, occurrences[0], key, occurrences))
    candidates.sort()

    consumed: dict[str, set[int]] = {path: set() for path in docs}
    claimed: set[tuple[str, int]] = set()
    findings: list[Finding] = []
    covered: set[tuple[str, int]] = set()

    for _, _, _, occurrences in candidates:
        live = [
            (path, start)
            for path, start in occurrences
            if (path, start) not in claimed
            and not consumed[path].intersection(range(start, start + min_lines))
        ]
        if len(set(live)) < 2:
            continue
        live = sorted(set(live))
        length = _window_extension(docs, live, min_lines)
        spans = tuple(
            Span(
                path=path,
                start_line=start + 1,
                end_line=start + length,
                text="\n".join(docs[path][start : start + length]),
            )
            for path, start in live
        )
        for path, start in live:
            consumed[path].update(range(start, start + length))
            covered.update((path, index) for index in range(start, start + length))
            claimed.update(
                (path, offset)
                for offset in range(start, start + length - min_lines + 1)
            )
        if len(spans[0].text) < min_chars:
            continue
        findings.append(
            Finding(
                kind=KIND_DUPLICATE,
                fix_class=FIX_SAFE,
                text=spans[0].text,
                spans=spans,
                rationale=(
                    "exact duplicate block (identical after whitespace "
                    "normalisation) — removing extra copies cannot lose meaning"
                ),
            )
        )

    if include_near:
        findings.extend(
            _find_near_duplicates(
                docs,
                min_lines=min_lines,
                min_chars=min_chars,
                near_ratio=near_ratio,
                covered=covered,
            )
        )

    findings.sort(
        key=lambda finding: (finding.spans[0].path, finding.spans[0].start_line)
    )
    return findings


def _find_near_duplicates(
    docs: Mapping[str, list[str]],
    *,
    min_lines: int,
    min_chars: int,
    near_ratio: float,
    covered: set[tuple[str, int]],
) -> list[Finding]:
    """Report similar-but-not-identical blocks that need operator judgement."""
    windows: list[tuple[str, int, str]] = []
    for path in sorted(docs):
        lines = docs[path]
        for start in range(len(lines) - min_lines + 1):
            span = range(start, start + min_lines)
            if any(not lines[index].strip() for index in span):
                continue
            if any((path, index) in covered for index in span):
                continue
            text = "\n".join(lines[start : start + min_lines])
            if len(text) < min_chars:
                continue
            windows.append((path, start, text))

    # Compare only windows that share at least one identical line: cheap
    # inverted index keeps the pairwise pass tractable on real skill trees.
    line_index: dict[str, list[int]] = {}
    for position, (_, _, text) in enumerate(windows):
        for line in {raw.strip() for raw in text.splitlines()}:
            line_index.setdefault(line, []).append(position)

    pairs: set[tuple[int, int]] = set()
    for members in line_index.values():
        for left in range(len(members)):
            for right in range(left + 1, len(members)):
                first, second = members[left], members[right]
                path_a, start_a, text_a = windows[first]
                path_b, start_b, text_b = windows[second]
                if text_a == text_b:
                    continue  # exact duplicates are reported separately
                if path_a == path_b and abs(start_a - start_b) < min_lines:
                    continue  # overlapping windows are not duplicates
                pairs.add((min(first, second), max(first, second)))

    findings: list[Finding] = []
    for first, second in sorted(pairs):
        path_a, start_a, text_a = windows[first]
        path_b, start_b, text_b = windows[second]
        ratio = SequenceMatcher(None, text_a, text_b).ratio()
        if ratio < near_ratio:
            continue
        findings.append(
            Finding(
                kind=KIND_NEAR_DUPLICATE,
                fix_class=FIX_RECOMMENDATION,
                text=text_a,
                spans=(
                    Span(path_a, start_a + 1, start_a + min_lines, text_a),
                    Span(path_b, start_b + 1, start_b + min_lines, text_b),
                ),
                rationale=(
                    f"normalised similarity {ratio:.2f} ≥ {near_ratio:.2f} but "
                    "blocks are not identical — removing or merging a copy "
                    "needs operator judgement"
                ),
            )
        )
    findings.sort(
        key=lambda finding: (finding.spans[0].path, finding.spans[0].start_line)
    )
    return findings


def _match_workflow_comment(line: str) -> str | None:
    """Return the matched rule label for *line*, or ``None``."""
    for label, pattern in WORKFLOW_COMMENT_RULES:
        if pattern.search(line):
            return label
    return None


def find_nonessential_ids(
    documents: Mapping[str, str],
    *,
    allow_ids: Iterable[str] = (),
) -> list[Finding]:
    """Report work-item IDs that are not on the essential allow-list."""
    allowed = frozenset(allow_ids)
    findings: list[Finding] = []
    for path, lines in sorted(_lines_by_path(documents).items()):
        for index, line in enumerate(lines, start=1):
            for match in WORK_ITEM_ID_RE.finditer(line):
                work_item_id = match.group(0)
                if work_item_id in allowed:
                    continue
                findings.append(
                    Finding(
                        kind=KIND_NONESSENTIAL_ID,
                        fix_class=FIX_SAFE,
                        text=work_item_id,
                        spans=(Span(path, index, index, line),),
                        rationale=(
                            "work-item ID is workflow traceability, not an "
                            "instruction; allow-list it to keep it"
                        ),
                    )
                )
    return findings


def find_workflow_comments(documents: Mapping[str, str]) -> list[Finding]:
    """Report workflow-comment noise lines (one finding per matching line)."""
    findings: list[Finding] = []
    for path, lines in sorted(_lines_by_path(documents).items()):
        for index, line in enumerate(lines, start=1):
            if _match_workflow_comment(line) is None:
                continue
            findings.append(
                Finding(
                    kind=KIND_WORKFLOW_COMMENT,
                    fix_class=FIX_SAFE,
                    text=line,
                    spans=(Span(path, index, index, line),),
                    rationale="workflow-traceability comment line, not instruction",
                )
            )
    return findings


def scan_documents(
    documents: Mapping[str, str],
    *,
    allow_ids: Iterable[str] = (),
    include_near: bool = False,
) -> list[Finding]:
    """Run every detector and return findings in a stable order."""
    findings = (
        find_duplicates(documents, include_near=include_near)
        + find_nonessential_ids(documents, allow_ids=allow_ids)
        + find_workflow_comments(documents)
    )
    findings.sort(
        key=lambda finding: (
            finding.spans[0].path,
            finding.spans[0].start_line,
            finding.kind,
        )
    )
    return findings


def strip_nonessential_ids(
    text: str,
    *,
    allow_ids: Iterable[str] = (),
) -> tuple[str, list[str]]:
    """Remove non-allow-listed work-item IDs from *text*.

    Returns the cleaned text and the removed IDs (in document order). A line
    that held nothing but traceability (e.g. ``(SA-0…)``) is dropped; empty
    ``()``/``[]`` pairs left behind by an inline ID are removed and runs of
    spaces collapsed, so surrounding prose stays readable.
    """
    allowed = frozenset(allow_ids)
    removed: list[str] = []
    kept: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        work_item_id = match.group(0)
        if work_item_id in allowed:
            return work_item_id
        removed.append(work_item_id)
        return ""

    for line in text.splitlines():
        cleaned = WORK_ITEM_ID_RE.sub(_replace, line)
        if cleaned != line:
            cleaned = re.sub(r"\(\s*\)", "", cleaned)
            cleaned = re.sub(r"\[\s*\]", "", cleaned)
            cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).rstrip()
        if cleaned.strip() or not line.strip():
            kept.append(cleaned)

    result = "\n".join(kept)
    if text.endswith("\n") and result:
        result += "\n"
    return result, removed


def strip_workflow_comments(text: str) -> tuple[str, list[str]]:
    """Remove workflow-comment noise lines from *text*."""
    removed: list[str] = []
    kept: list[str] = []
    for line in text.splitlines():
        if _match_workflow_comment(line) is not None:
            removed.append(line)
            continue
        kept.append(line)
    result = "\n".join(kept)
    if text.endswith("\n") and result:
        result += "\n"
    return result, removed
