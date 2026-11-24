"""Deterministic detector core for the skill-linter skill.

Work item: SA-0MWGAYPMX002Z0VC (child of SA-0MWFP1E9P0027EOE). This module is
the deterministic half of the linter: regex + structural comparison only, no
LLM dependency. The CLI (report generator, ``--apply``, ``--install-cron``)
wraps these primitives (report/CLI contract pinned by
``tests/test_lint_skills.py``; work item SA-0MWGAZ286009G2JR).

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

Scan scope (AC2): ``discover_documents(root)`` reads the default scope —
``skill/*/SKILL.md`` plus the repo-root ``AGENTS.md`` / ``AGENTS_GLOBAL.md``.
Passing ``targets`` (the CLI's ``--target``) replaces that scope: a file is
read as-is, a directory expands recursively over ``SKILL.md`` / ``AGENTS.md`` /
``AGENTS_GLOBAL.md``, and hidden directories are never traversed. Thresholds and
the allow-list travel in ``ScanConfig`` (defaults: the module constants and
``DEFAULT_ALLOW_IDS``); ``scan_paths(root, targets=…, config=…)`` joins
discovery and scanning for the CLI to wrap.

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

import argparse
import json
import re
import shlex
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

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

#: Default scan scope, relative to the repository root (AC2).
DEFAULT_SKILL_GLOB = "skill/*/SKILL.md"
DEFAULT_ROOT_DOCUMENTS = ("AGENTS.md", "AGENTS_GLOBAL.md")

#: File names a directory ``--target`` expands to.
SCAN_DOCUMENT_NAMES = frozenset({"SKILL.md", *DEFAULT_ROOT_DOCUMENTS})

#: Allow-list default: no work-item ID is essential unless the caller says so.
DEFAULT_ALLOW_IDS: tuple[str, ...] = ()

#: Report schema version — bumped only on an incompatible JSON shape change.
REPORT_SCHEMA_VERSION = 1

#: CLI exit codes (AC6): clean, findings present, usage/scan error.
EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

#: ``--install-cron`` success is exit 0 on its own path; kept as a named
#: constant so callers never equate "cron printed" with "tree clean".
EXIT_CRON_OK = 0

#: Cron schedule printed by ``--install-cron weekly`` (Mondays at 03:00).
CRON_SCHEDULE_WEEKLY = "0 3 * * 1"
CRON_SCHEDULES = {"weekly": CRON_SCHEDULE_WEEKLY}

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
    min_lines: int = MIN_DUPLICATE_LINES,
    min_chars: int = MIN_DUPLICATE_CHARS,
    near_ratio: float = NEAR_DUPLICATE_RATIO,
) -> list[Finding]:
    """Run every detector and return findings in a stable order."""
    findings = (
        find_duplicates(
            documents,
            min_lines=min_lines,
            min_chars=min_chars,
            include_near=include_near,
            near_ratio=near_ratio,
        )
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


@dataclass(frozen=True)
class ScanConfig:
    """Detector thresholds and allow-list for one scan run (AC2).

    Defaults mirror the module constants, so ``ScanConfig()`` reproduces
    ``scan_documents``' documented behaviour. ``allow_ids`` names the
    work-item IDs that are essential: they are reported by neither the ID
    detector nor the applicator, and survive adaptation.
    """

    min_lines: int = MIN_DUPLICATE_LINES
    min_chars: int = MIN_DUPLICATE_CHARS
    include_near: bool = False
    near_ratio: float = NEAR_DUPLICATE_RATIO
    allow_ids: tuple[str, ...] = DEFAULT_ALLOW_IDS

    def __post_init__(self) -> None:
        if self.min_lines < 2:
            raise ValueError(
                f"min_lines must be at least 2, got {self.min_lines}: a shorter "
                "window cannot distinguish a duplicate block from boilerplate"
            )
        if self.min_chars < 0:
            raise ValueError(f"min_chars must be non-negative, got {self.min_chars}")
        if not 0.0 < self.near_ratio <= 1.0:
            raise ValueError(f"near_ratio must be in (0, 1], got {self.near_ratio}")
        if isinstance(self.allow_ids, str):
            raise TypeError(
                "allow_ids must be an iterable of work-item IDs, not a single "
                f"string: {self.allow_ids!r}"
            )
        allow_ids = tuple(self.allow_ids)
        if not all(isinstance(entry, str) for entry in allow_ids):
            raise ValueError(f"allow_ids must contain strings, got {allow_ids!r}")
        object.__setattr__(self, "allow_ids", allow_ids)

    def scan(self, documents: Mapping[str, str]) -> list[Finding]:
        """Report findings for *documents* using this configuration."""
        return scan_documents(
            documents,
            allow_ids=self.allow_ids,
            include_near=self.include_near,
            min_lines=self.min_lines,
            min_chars=self.min_chars,
            near_ratio=self.near_ratio,
        )


def _relative_key(root: Path, path: Path) -> str:
    """Return *path* relative to *root* in POSIX form (absolute if outside)."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _has_hidden_part(path: Path, base: Path) -> bool:
    """True when *path* sits under a dot-directory below *base*."""
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        return False
    return any(part.startswith(".") for part in parts[:-1])


def _default_scan_paths(root: Path) -> list[Path]:
    """Files in the default scope: ``skill/*/SKILL.md`` + root AGENTS docs."""
    paths = [
        path
        for path in sorted(root.glob(DEFAULT_SKILL_GLOB))
        if path.is_file() and not _has_hidden_part(path, root)
    ]
    paths.extend(
        root / name for name in DEFAULT_ROOT_DOCUMENTS if (root / name).is_file()
    )
    return paths


def _expand_target(candidate: Path) -> Iterator[Path]:
    """Yield the documents a single ``--target`` covers."""
    if candidate.is_file():
        yield candidate
        return
    for path in sorted(candidate.rglob("*")):
        if (
            path.is_file()
            and path.name in SCAN_DOCUMENT_NAMES
            and not _has_hidden_part(path, candidate)
        ):
            yield path


def _read_document(path: Path) -> str:
    """Read one scan document, failing loudly on a non-UTF-8 file."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"scan target is not valid UTF-8 text: {path} ({exc}); convert it or "
            "exclude it with --target"
        ) from exc


def discover_documents(
    root: str | Path,
    *,
    targets: Iterable[str | Path] | None = None,
) -> dict[str, str]:
    """Read the documents in scan scope, keyed by root-relative POSIX path.

    Args:
        root: Repository root the default scope is resolved against and that
            relative ``targets`` are joined to.
        targets: ``--target`` override. ``None`` uses the default scope
            (``skill/*/SKILL.md`` + repo-root ``AGENTS.md`` /
            ``AGENTS_GLOBAL.md``); any value replaces it entirely. Each
            target is a file (read as-is, even outside the root) or a
            directory (expanded recursively over ``SKILL.md`` / ``AGENTS.md``
            / ``AGENTS_GLOBAL.md``, skipping hidden directories).

    Returns:
        Mapping of root-relative POSIX path → file text, ordered by path.
        Missing files in the *default* scope are simply absent; a missing
        explicit *target* raises ``FileNotFoundError``.

    Raises:
        FileNotFoundError: a ``--target`` path does not exist.
        ValueError: a scanned file is not valid UTF-8.
    """
    root_path = Path(root).expanduser()

    if targets is None:
        paths = _default_scan_paths(root_path)
    else:
        paths = []
        for target in targets:
            candidate = Path(target).expanduser()
            if not candidate.is_absolute():
                candidate = root_path / candidate
            if not candidate.exists():
                raise FileNotFoundError(
                    f"scan target does not exist: {candidate} — pass an existing "
                    "file or directory with --target"
                )
            paths.extend(_expand_target(candidate))

    documents: dict[str, str] = {}
    for path in paths:
        documents.setdefault(_relative_key(root_path, path), _read_document(path))
    return dict(sorted(documents.items()))


def scan_paths(
    root: str | Path,
    *,
    targets: Iterable[str | Path] | None = None,
    config: ScanConfig | None = None,
) -> list[Finding]:
    """Discover the scan scope under *root* and report findings.

    The end-to-end entry point the CLI wraps: ``targets`` is its ``--target``
    override and ``config`` carries the thresholds and allow-list defaults.
    """
    effective = ScanConfig() if config is None else config
    return effective.scan(discover_documents(root, targets=targets))


#: Markdown structure that can wrap a traceability-only line: list bullets,
#: ordered-list numbers, task markers and blockquote markers.
_STRUCTURAL_PREFIX_RE = re.compile(
    r"^\s*(?:(?:[-*+]|\d+[.)])\s*|\[ \]\s*|\[[xX]\]\s*|>+\s*)+"
)

#: Bracket pairs left empty by an inline ID removal (``()``, ``[]``, ``<>``).
_EMPTY_PAIR_RE = re.compile(r"\(\s*\)|\[\s*\]|<\s*>|\{\s*\}")

#: Whitespace left before punctuation when the text it preceded was an ID.
_SPACE_BEFORE_PUNCT_RE = re.compile(r"[ \t]+([,.;:!?)\]}>])")


def _tidy_after_id_removal(original: str, cleaned: str) -> str:
    """Tidy the whitespace and empty pairs an inline ID removal leaves behind."""
    cleaned = _EMPTY_PAIR_RE.sub("", cleaned)
    cleaned = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).rstrip()
    if original[:1] not in (" ", "\t"):
        cleaned = cleaned.lstrip(" \t")
    return cleaned


def _holds_content(line: str) -> bool:
    """True when *line* carries text beyond Markdown structure and punctuation.

    Traceability-only lines such as ``- (SA-0…)``, ``1. SA-0…`` or
    ``| SA-0… |`` must disappear entirely rather than leave a stray bullet,
    list number, quote marker or empty table row behind.
    """
    residual = _STRUCTURAL_PREFIX_RE.sub("", line)
    return any(character.isalnum() for character in residual)


def strip_nonessential_ids(
    text: str,
    *,
    allow_ids: Iterable[str] = (),
) -> tuple[str, list[str]]:
    """Remove non-allow-listed work-item IDs from *text*.

    Returns the cleaned text and the removed IDs (in document order). A line
    that held nothing but traceability (e.g. ``(SA-0…)``, ``- (SA-0…)``,
    ``1. SA-0…``, ``| SA-0… |``) is dropped whole; empty ``()``/``[]`` pairs
    left behind by an inline ID are removed, whitespace before punctuation is
    closed up and runs of spaces are collapsed, so surrounding prose stays
    readable.
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
            cleaned = _tidy_after_id_removal(line, cleaned)
            if not _holds_content(cleaned):
                continue
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


# ── Report generation ──────────────────────────────────────────────────────


_HEADING_RE = re.compile(r"^#{1,6}\s+\S")


def section_for_line(text: str, line_number: int) -> str:
    """Return the nearest Markdown heading at or above *line_number*.

    ``(document top)`` is returned when no heading precedes the line, so the
    report always names a section (AC1: file, section/line span, text).
    """
    lines = text.splitlines()
    if line_number < 1:
        return "(document top)"
    for index in range(min(line_number, len(lines)) - 1, -1, -1):
        if _HEADING_RE.match(lines[index]):
            return lines[index].strip()
    return "(document top)"


def _proposed_text(finding: Finding) -> str:
    """The text a safe fix would leave behind (recommendations: first copy)."""
    if finding.kind == KIND_WORKFLOW_COMMENT:
        return ""
    if finding.kind == KIND_NONESSENTIAL_ID:
        cleaned, _ = strip_nonessential_ids(finding.spans[0].text)
        return cleaned
    return finding.spans[0].text


def _action_text(finding: Finding) -> str:
    """Human-readable description of what a fix would do (AC2/AC3/AC4)."""
    if finding.kind == KIND_DUPLICATE:
        return "would remove duplicate copies, keeping the first occurrence"
    if finding.kind == KIND_NEAR_DUPLICATE:
        return (
            "would merge or shorten near-identical copies — requires explicit "
            "operator approval, never applied automatically"
        )
    if finding.kind == KIND_NONESSENTIAL_ID:
        return f"would strip work-item ID {finding.text}"
    if finding.kind == KIND_WORKFLOW_COMMENT:
        return "would remove the workflow-comment line"
    return "would fix the finding"


def _finding_entry(finding: Finding, documents: Mapping[str, str]) -> dict:
    """Project one finding into the stable report-entry schema (AC1)."""
    first = finding.spans[0]
    return {
        "kind": finding.kind,
        "fix_class": finding.fix_class,
        "path": first.path,
        "start_line": first.start_line,
        "end_line": first.end_line,
        "section": section_for_line(documents.get(first.path, ""), first.start_line),
        "text": finding.text,
        "proposed_text": _proposed_text(finding),
        "rationale": finding.rationale,
        "requires_approval": finding.fix_class == FIX_RECOMMENDATION,
        "action": _action_text(finding),
        "spans": [span.to_dict() for span in finding.spans],
    }


def build_report(
    findings: Sequence[Finding],
    documents: Mapping[str, str],
    *,
    root: str | Path = ".",
    dry_run: bool = True,
    applied: int = 0,
) -> dict:
    """Build the machine-readable report payload (AC1/AC2/AC3)."""
    entries = [_finding_entry(finding, documents) for finding in findings]
    safe = sum(1 for finding in findings if finding.fix_class == FIX_SAFE)
    recommendation = sum(
        1 for finding in findings if finding.fix_class == FIX_RECOMMENDATION
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "root": str(Path(root).expanduser().resolve()),
        "dry_run": dry_run,
        "findings": entries,
        "summary": {
            "total": len(entries),
            "safe": safe,
            "recommendation": recommendation,
            "applied": applied,
            "remaining": len(entries),
        },
    }


def render_text_report(report: Mapping) -> str:
    """Render a report as the human-readable text form (AC1/AC2)."""
    lines = [
        f"skill-linter report ({'dry-run' if report['dry_run'] else 'apply'})",
        f"root: {report['root']}",
    ]
    if report["dry_run"]:
        lines.append("(dry-run: no files modified)")
    entries = report["findings"]
    if not entries:
        lines.append("")
        lines.append("no findings — clean")
    for entry in entries:
        lines.append("")
        lines.append(
            f"{entry['path']}:{entry['start_line']}-{entry['end_line']}  "
            f"{entry['kind']}  [fix class: {entry['fix_class']}]  "
            f"section: {entry['section']}"
        )
        lines.append("  current text:")
        for current in entry["text"].splitlines() or [""]:
            lines.append(f"    {current}")
        lines.append("  proposed text:")
        if entry["proposed_text"]:
            for proposed in entry["proposed_text"].splitlines():
                lines.append(f"    {proposed}")
        else:
            lines.append("    (line removed)")
        lines.append(f"  action: {entry['action']}")
        lines.append(f"  rationale: {entry['rationale']}")
        if entry["requires_approval"]:
            lines.append(
                "  approval: required — operator must approve; never auto-applied"
            )
    summary = report["summary"]
    lines.append("")
    lines.append(
        f"summary: {summary['total']} finding(s) — {summary['safe']} safe, "
        f"{summary['recommendation']} recommendation, {summary['applied']} "
        f"applied, {summary['remaining']} remaining"
    )
    return "\n".join(lines)


def render_json_report(report: Mapping) -> str:
    """Render a report as deterministic, parseable JSON (AC1)."""
    return json.dumps(report, indent=2, sort_keys=False)


# ── Safe-fix application ───────────────────────────────────────────────────


def apply_safe_fixes(
    documents: Mapping[str, str],
    findings: Iterable[Finding],
    *,
    allow_ids: Iterable[str] = (),
) -> dict[str, str]:
    """Apply only ``FIX_SAFE`` findings and return the changed documents.

    Recommendation-class findings are ignored: they need explicit operator
    approval (AC3/AC4). Duplicate copies beyond the first are deleted, then
    non-allow-listed work-item IDs and workflow-comment lines are stripped.
    Nothing is written to disk here — the CLI writes the returned mapping.
    """
    remove_lines: dict[str, set[int]] = {}
    for finding in findings:
        if finding.fix_class != FIX_SAFE or finding.kind != KIND_DUPLICATE:
            continue
        for span in finding.spans[1:]:
            remove_lines.setdefault(span.path, set()).update(
                range(span.start_line, span.end_line + 1)
            )

    changed: dict[str, str] = {}
    for path, text in documents.items():
        lines = text.splitlines()
        dropped = remove_lines.get(path, set())
        kept = "\n".join(
            line for index, line in enumerate(lines, start=1) if index not in dropped
        )
        if text.endswith("\n") and kept:
            kept += "\n"
        cleaned, _ = strip_nonessential_ids(kept, allow_ids=allow_ids)
        cleaned, _ = strip_workflow_comments(cleaned)
        if cleaned != text:
            changed[path] = cleaned
    return changed


# ── Cron helper ────────────────────────────────────────────────────────────


def cron_line(root: str | Path, script: str | Path, *, log_path: str | Path | None = None) -> str:
    """Return the weekly dry-run crontab line printed by ``--install-cron``.

    The line runs the linter with ``--dry-run --json``, appending to a log
    under ``.worklog/`` (AC5). Printing it never installs anything.
    """
    root_path = Path(root).expanduser().resolve()
    script_path = Path(script).expanduser().resolve()
    log = (
        Path(log_path).expanduser()
        if log_path is not None
        else root_path / ".worklog" / "skill-linter-cron.log"
    )
    return (
        f"{CRON_SCHEDULE_WEEKLY} cd {shlex.quote(str(root_path))} && "
        f"{shlex.quote(sys.executable)} {shlex.quote(str(script_path))} "
        f"--dry-run --json >> {shlex.quote(str(log))} 2>&1"
    )


# ── CLI ────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lint_skills.py",
        description=(
            "Scan SKILL.md / AGENTS documents for duplicated content and "
            "workflow traceability noise. Dry-run by default; --apply removes "
            "only safe-fix findings."
        ),
    )
    parser.add_argument("--root", default=".", help="repository root to scan")
    parser.add_argument(
        "--target",
        action="append",
        default=None,
        metavar="PATH",
        help="file or directory to scan (repeatable; replaces the default scope)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply safe-fix findings (recommendations are only reported)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without modifying files (the default)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit the machine-readable JSON report",
    )
    parser.add_argument(
        "--install-cron",
        nargs="?",
        const="weekly",
        default=None,
        metavar="SCHEDULE",
        help="print a crontab line for a scheduled dry-run report (weekly)",
    )
    parser.add_argument(
        "--allow-id",
        action="append",
        default=[],
        metavar="ID",
        help="work-item ID that is essential and must be preserved",
    )
    parser.add_argument(
        "--min-lines",
        type=int,
        default=MIN_DUPLICATE_LINES,
        help="minimum lines in a duplicate block",
    )
    parser.add_argument(
        "--min-chars",
        type=int,
        default=MIN_DUPLICATE_CHARS,
        help="minimum characters in a duplicate block",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the process exit code (AC6)."""
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.install_cron is not None:
        schedule = args.install_cron
        if schedule not in CRON_SCHEDULES:
            print(
                f"lint_skills: error: unknown cron schedule {schedule!r}; "
                f"supported: {', '.join(sorted(CRON_SCHEDULES))}",
                file=sys.stderr,
            )
            return EXIT_ERROR
        line = cron_line(args.root, Path(__file__))
        print(
            "Weekly skill-linter dry-run crontab line "
            "(printed only; NOT installed — add it with `crontab -e`):"
        )
        print()
        print(line)
        return EXIT_CRON_OK

    try:
        config = ScanConfig(
            min_lines=args.min_lines,
            min_chars=args.min_chars,
            include_near=True,
            allow_ids=tuple(args.allow_id),
        )
    except (TypeError, ValueError) as exc:
        print(f"lint_skills: error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    root = Path(args.root).expanduser()
    try:
        documents = discover_documents(root, targets=args.target)
    except (FileNotFoundError, ValueError) as exc:
        print(f"lint_skills: error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    findings = config.scan(documents)
    apply_requested = args.apply and not args.dry_run
    applied = 0
    if apply_requested:
        changed = apply_safe_fixes(documents, findings, allow_ids=config.allow_ids)
        applied = sum(1 for finding in findings if finding.fix_class == FIX_SAFE)
        for path, text in changed.items():
            target = Path(path)
            if not target.is_absolute():
                target = root / target
            target.write_text(text, encoding="utf-8")
        documents.update(changed)
        findings = config.scan(documents)

    report = build_report(
        findings,
        documents,
        root=root,
        dry_run=not apply_requested,
        applied=applied,
    )
    if args.as_json:
        print(render_json_report(report))
    else:
        print(render_text_report(report))
    return EXIT_CLEAN if not findings else EXIT_FINDINGS


if __name__ == "__main__":  # pragma: no cover - exercised via main()
    raise SystemExit(main())
