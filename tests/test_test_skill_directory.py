"""Deterministic conformance gate for the Test execution policy (AC3).

Work item: SA-0MV7TS8LT007FMOW (parent SA-0MTYIMWQP0020ZUC).

The Test execution policy (AGENTS_GLOBAL.md, AC1) makes it an absolute,
no-exceptions rule that agents never invoke test commands directly — every
agent-initiated test run goes through the test skill (`/skill:test`,
`run_tests.py`). This test turns that policy into a deterministic,
repo-wide gate: it scans every agent-facing doc (AGENTS_GLOBAL.md,
skill/*/SKILL.md, command/*.md) and FAILS when a *directive* to run a
prohibited test command (ad-hoc `pytest`, `npm test`, `npx vitest run`,
`node --test`, `python3 -m pytest`) is introduced that is not an explicitly
whitelisted infrastructure reference.

It follows the existing doc-conformance pattern of
tests/test_audit_skill_doc.py, tests/test_cleanup_skill_doc.py and
tests/test_test_skill_doc.py, extended from a single SKILL.md contract to a
whole-directory scan (AC3 key file: tests/test_test_skill_directory.py).

Design notes (keep this self-contained and deterministic — no network, no
subprocesses, no randomness):

- Patterns are *command-shaped* so references by other names ("the pytest
  suite", "pytest-style layout", "npm-test convention", `pytest.fail`) are
  not false positives (AC3: "regex scope targets agent-facing directive
  patterns only to avoid false positives on test-file imports").
- An occurrence is a *directive* — and therefore a violation — when any of:
  1. it sits inside a fenced code block (a runnable command shown in a
     ``` fence is a directive regardless of surrounding caveats);
  2. the line starts with the command (e.g. a bare "`` python3 -m pytest
     tests/ -q``" line re-imported into a `## Tests` section);
  3. an imperative verb (run / invoke / execute / use / call / prefer / try)
     immediately governs it, with no prohibition marker (never / no / not /
     don't / prohibit …) in the surrounding window.
  The policy's own wording (AC1, "never invoke … no ad-hoc `pytest` …") and
  the AC2 reworded references ("the policy prohibits direct `pytest`
  invocations") therefore pass by construction.
- Whitelist (explicit and commented, per AC3): the test skill's own runner
  docs (skill/test/SKILL.md), git hooks/CI references (pre-push hook /
  `--scope full`), audit F2 suite-command resolution prose
  (skill/audit/SKILL.md, SA-0MSTMYE79006NA61), and the single known
  resolve-pr-comments snippet tracked by SA-0MSN6FBFF009VCOB. The whitelists
  are additive and narrow: any *other* occurrence in those files is flagged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# ── Prohibited command-shaped directive patterns (AC3) ──────────────────────
# Command-shaped, word-boundary-aware: `pytest.fail`, "pytest-style layout",
# ".pytest_cache", "npm-test convention" and the like are NOT matches.
_PROHIBITED_COMMAND = re.compile(
    r"""
    python3?\s+-m\s+pytest                          # python3 -m pytest
    | \bnpm\s+(?:--silent\s+)?(?:run\s+)?test\b     # npm test / npm --silent test / npm run test
    | \bnpx\s+vitest\s+run\b                        # npx vitest run
    | \bnode\s+--test\b                             # node --test
    | (?<![\w.-])pytest(?![\w.-])                   # bare pytest command word
    """,
    re.VERBOSE,
)

# Markers of prohibition/policy phrasing around a mention of a test command
# (e.g. AGENTS_GLOBAL.md's "never invoke … no ad-hoc `pytest` …" and AC2's
# "the policy prohibits direct `pytest` invocations"). `\bno\b` intentionally
# does not match inside "node"/"nothing" (word-boundary aware).
_PROHIBITION = re.compile(
    r"\b(?:never\b|no\b|not\b|don'?t|mustn'?t|prohibit\w*|forbid\w*|banned|"
    r"avoid|unless|instead|ad-hoc|ad\s+hoc)\b",
    re.IGNORECASE,
)

# Imperative verbs that turn a command mention into an instruction.
_DIRECTIVE_VERB = re.compile(
    r"\b(?:run|invoke|execute|use|call|prefer|try)\b", re.IGNORECASE
)

_FENCE_RE = re.compile(r"^\s*```")

_PROHIBITION_WINDOW_BEFORE = 120  # chars before the match where prohibition
_PROHIBITION_WINDOW_AFTER = 0  # wording appears ("X prohibits `pytest` …")
_VERB_WINDOW = 60  # chars before the match where a directive verb may sit


@dataclass(frozen=True)
class Violation:
    """A flagged direct test-command directive inside an agent-facing doc."""

    rel_path: str
    line_no: int
    directive: str
    line: str


# ── Whitelist (AC3): legitimate infrastructure references ──────────────────
# Each entry is deliberately narrow: it covers exactly the sanctioned
# infrastructure prose and nothing else, so a genuinely new directive in the
# same file still fails the gate.


def _is_test_skill_runner_doc(path: Path) -> bool:
    """Whitelist: the test skill's own runner docs (skill/test/SKILL.md).

    This file documents the runner the policy *mandates* — the commands it
    resolves (`pytest -q -r a --disable-warnings`, `npm --silent test`,
    `node --test "tests/**"` etc.) and the pre-push/`implement.py finish`
    gates. Infrastructure, not agent directives.
    """
    return path.parts[-3:] == ("skill", "test", "SKILL.md")


def _is_hook_or_ci_context(line: str) -> bool:
    """Whitelist: git hooks / CI gates (the pre-push hook and
    `implement.py finish` running `--scope full`) are infrastructure — the
    policy explicitly calls them "infrastructure, not agent runs, and out of
    scope" (AGENTS_GLOBAL.md, Test execution policy).
    """
    return "pre-push hook" in line or "--scope full" in line


def _is_audit_f2_suite_resolution_line(path: Path, line: str) -> bool:
    """Whitelist: audit F2 suite-command resolution prose
    (skill/audit/SKILL.md, SA-0MSTMYE79006NA61) — describes the convention
    detection the audit runner performs (extension file > npm-test convention
    > pytest > node dirs). Infrastructure, not an agent directive.
    """
    return path.parts[-2:] == ("audit", "SKILL.md") and "F2" in line


def _is_resolve_pr_comments_snippet(path: Path, line: str) -> bool:
    """Whitelist: the single known direct-test-command snippet in
    skill/resolve-pr-comments/SKILL.md ("never `npm --silent test` directly,
    which bypasses the per-repo test cache"), tracked for rewrite by
    SA-0MSN6FBFF009VCOB. Only that exact snippet — a new directive in the
    same file is still flagged.
    """
    return (
        path.parts[-2:] == ("resolve-pr-comments", "SKILL.md")
        and "--silent test" in line
        and "bypasses the per-repo test cache" in line
    )


def _is_whitelisted(path: Path, line: str) -> bool:
    return (
        _is_test_skill_runner_doc(path)
        or _is_hook_or_ci_context(line)
        or _is_audit_f2_suite_resolution_line(path, line)
        or _is_resolve_pr_comments_snippet(path, line)
    )


# ── Directive detection helpers ─────────────────────────────────────────────


def _has_prohibition_in_window(context: str, start: int, end: int) -> bool:
    lo = max(0, start - _PROHIBITION_WINDOW_BEFORE)
    hi = min(len(context), end + _PROHIBITION_WINDOW_AFTER)
    return _PROHIBITION.search(context[lo:hi]) is not None


def _has_directive_verb_before(context: str, start: int) -> bool:
    lo = max(0, start - _VERB_WINDOW)
    return _DIRECTIVE_VERB.search(context[lo:start]) is not None


def _starts_with_prohibited_command(line: str) -> bool:
    """A bare command line (e.g. a `` python3 -m pytest tests/ -q`` line in
    a `## Tests` section) is a directive even without an imperative verb."""
    stripped = line.strip().lstrip("`")
    m = _PROHIBITED_COMMAND.search(stripped)
    return m is not None and m.start() == 0


# Context lookback for prohibition/verb phrasing that wraps across lines
# (e.g. test-churn/SKILL.md: "…prohibits direct" then "`pytest`
# invocations." on the following line).
_CONTEXT_LOOKBACK = 80


def _match_context(prev_line: str | None, line: str) -> tuple[str, int]:
    """Combine the tail of the previous line with the current line so
    prohibition/verb windows can see a phrase split across a line break.
    Returns (context, offset) where offset maps `line` positions into
    `context` positions.

    Joined with a newline (not bare concatenation) so a word at the start of
    `line` keeps its ``\\b`` word boundary against the previous line's last
    word."""
    if prev_line is None:
        return line, 0
    tail = prev_line[-_CONTEXT_LOOKBACK:]
    return tail + "\n" + line, len(tail) + 1


def _repo_relative_label(path: Path) -> str:
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:  # fixture paths outside the repo still get a label
        return str(path)


# ── The scan ────────────────────────────────────────────────────────────────


def scan_docs(paths: list[Path]) -> list[Violation]:
    """Return every prohibited direct test-command directive found in the
    given docs (whitelisted infrastructure references excluded)."""
    violations: list[Violation] = []
    for path in sorted(set(paths)):
        rel = _repo_relative_label(path)
        lines = path.read_text(encoding="utf-8").splitlines()
        in_fence = False
        prev_line: str | None = None
        for line_no, line in enumerate(lines, start=1):
            if _FENCE_RE.match(line):
                in_fence = not in_fence
                prev_line = line
                continue  # a fence marker itself is never a command directive
            context, offset = _match_context(prev_line, line)
            for m in _PROHIBITED_COMMAND.finditer(line):
                if _is_whitelisted(path, line):
                    continue
                start, end = offset + m.start(), offset + m.end()
                if in_fence:
                    violations.append(Violation(rel, line_no, m.group(0), line))
                    continue
                if _has_prohibition_in_window(context, start, end):
                    continue  # policy/prohibition phrasing, not a directive
                if _starts_with_prohibited_command(line):
                    violations.append(Violation(rel, line_no, m.group(0), line))
                    continue
                if _has_directive_verb_before(context, start):
                    violations.append(Violation(rel, line_no, m.group(0), line))
            prev_line = line
    return violations


def _format_violations(violations: list[Violation]) -> str:
    parts = ["Agent-facing docs contain prohibited direct test-command directive(s):"]
    for v in violations:
        parts.append(f"  {v.rel_path}:{v.line_no}: {v.directive!r}")
        parts.append(f"      {v.line.strip()}")
    parts.append(
        "Fix: route the run through the test skill (/skill:test, run_tests.py) "
        "per the Test execution policy, or — for a genuinely legitimate "
        "infrastructure reference — extend the explicitly-commented whitelist "
        "in this file."
    )
    return "\n".join(parts)


def _agent_facing_docs() -> list[Path]:
    """AGENTS_GLOBAL.md, skill/*/SKILL.md and command/*.md (non-recursive)."""
    docs = [_REPO_ROOT / "AGENTS_GLOBAL.md"]
    docs += sorted((_REPO_ROOT / "skill").glob("*/SKILL.md"))
    docs += sorted((_REPO_ROOT / "command").glob("*.md"))
    return [p for p in docs if p.is_file()]


# ── The conformance gate ────────────────────────────────────────────────────


def test_no_prohibited_direct_test_command_directives() -> None:
    """AC3: no agent-facing doc may carry a prohibited direct test-command
    directive that is not an explicitly whitelisted infrastructure reference.

    Scans AGENTS_GLOBAL.md, skill/*/SKILL.md and command/*.md. Fails on any
    directive (ad-hoc pytest / npm test / npx vitest run / node --test).
    """
    violations = scan_docs(_agent_facing_docs())
    assert not violations, _format_violations(violations)


def test_scan_covers_all_agent_facing_docs() -> None:
    """The scan surface must stay populated — a doc-restructure cannot
    silently empty the gate (determinism of the coverage claim)."""
    docs = _agent_facing_docs()
    rels = [_repo_relative_label(p) for p in docs]
    assert "AGENTS_GLOBAL.md" in rels
    skills = [r for r in rels if r.startswith("skill/") and r.endswith("/SKILL.md")]
    assert len(skills) >= 10, "expected the full skill/*/SKILL.md set"
    commands = [r for r in rels if r.startswith("command/") and r.endswith(".md")]
    assert len(commands) >= 1, "expected command/*.md files to be scanned"


# ── Sanity: the gate must be able to fire (fixtures) ────────────────────────
# These are not self-referential simulations: they exercise scan_docs()'s
# observable behaviour on fixture docs so a future tightening of the regex
# that stops the gate from ever firing is caught immediately.


def _write_doc(tmp_path: Path, rel: str, text: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestScannerFlagsDirectives:
    """The scanner flags each prohibited command when used as a directive."""

    def test_flags_imperative_run_directive(self, tmp_path: Path) -> None:
        doc = _write_doc(tmp_path, "docs/test-run.md", "# T\nRun `pytest -q tests/` to verify.\n")
        violations = scan_docs([doc])
        assert len(violations) == 1
        assert "pytest" in violations[0].directive

    def test_flags_directive_verb_split_across_lines(self, tmp_path: Path) -> None:
        """An imperative verb wrapping onto the line above a lone command
        mention must still be caught (verb window spans line breaks)."""
        doc = _write_doc(
            tmp_path, "docs/test-verb-split.md", "# T\nRun\nsee `pytest -q tests/` now.\n"
        )
        assert len(scan_docs([doc])) == 1

    def test_flags_advisory_prefer_directive(self, tmp_path: Path) -> None:
        doc = _write_doc(tmp_path, "docs/test-prefer.md", "# T\nPrefer `npm --silent test` for local reproduction.\n")
        assert len(scan_docs([doc])) == 1

    def test_flags_fenced_command_directive(self, tmp_path: Path) -> None:
        doc = _write_doc(
            tmp_path, "docs/test-fenced.md",
            "# T\n## Tests\n```bash\npython3 -m pytest tests/ -q\n```\n",
        )
        violations = scan_docs([doc])
        assert len(violations) == 1
        assert "python3 -m pytest" in violations[0].directive

    def test_flags_bare_command_line(self, tmp_path: Path) -> None:
        doc = _write_doc(tmp_path, "docs/test-bare.md", "# T\n## Tests\n`npm test`\n")
        assert len(scan_docs([doc])) == 1

    def test_flags_npx_vitest_run_directive(self, tmp_path: Path) -> None:
        doc = _write_doc(tmp_path, "docs/test-vitest.md", "# T\nRun `npx vitest run --project unit`.\n")
        assert len(scan_docs([doc])) == 1

    def test_flags_node_test_directive(self, tmp_path: Path) -> None:
        doc = _write_doc(tmp_path, "docs/test-node.md", "# T\nInvoke `node --test tests/node`.\n")
        assert len(scan_docs([doc])) == 1

    def test_trailing_negation_does_not_mask_directive(self, tmp_path: Path) -> None:
        """A prohibition-marker word appearing AFTER the command (e.g. "do
        not skip") must not mask an imperative directive — the window looks
        before the command only."""
        doc = _write_doc(
            tmp_path, "docs/test-trailing-no.md",
            "# T\nRun `pytest -q`; do not skip it.\n",
        )
        assert len(scan_docs([doc])) == 1

    def test_trailing_instead_does_not_mask_directive(self, tmp_path: Path) -> None:
        doc = _write_doc(
            tmp_path, "docs/test-trailing-instead.md",
            "# T\nRun `npm test` instead of the test skill.\n",
        )
        assert len(scan_docs([doc])) == 1


class TestScannerAllowsLegitimateReferences:
    """The scanner passes the policy's own wording, AC2's reworded references
    and every explicit AC3 whitelist entry."""

    def test_policy_statement_is_not_a_directive(self, tmp_path: Path) -> None:
        doc = _write_doc(
            tmp_path, "docs/test-policy.md",
            "Agents must **never** invoke test commands directly — no ad-hoc "
            "`pytest`, `npm test`, `vitest`, `node --test`, or equivalent "
            "invocations.\n",
        )
        assert scan_docs([doc]) == []

    def test_prohibition_sentence_allowed(self, tmp_path: Path) -> None:
        doc = _write_doc(
            tmp_path, "docs/test-prohib.md",
            "AGENTS_GLOBAL.md prohibits direct `pytest` invocations.\n",
        )
        assert scan_docs([doc]) == []

    def test_prohibition_sentence_split_across_lines_allowed(self, tmp_path: Path) -> None:
        """test-churn/SKILL.md splits the prohibition across a line break
        ("…prohibits direct" / "`pytest` invocations.") — the window must
        span into the previous line."""
        doc = _write_doc(
            tmp_path, "docs/test-prohib-split.md",
            "Agent test execution must go through the test skill "
            "(`/skill:test`) —\nthe Test execution policy in AGENTS_GLOBAL.md "
            "prohibits direct\n`pytest` invocations.\n",
        )
        assert scan_docs([doc]) == []

    def test_hyphenated_npm_test_convention_not_matched(self, tmp_path: Path) -> None:
        doc = _write_doc(
            tmp_path, "docs/test-convention.md",
            "Resolution order: extension file > npm-test convention "
            "(`npm --silent test`) > pytest (only when the repo declares a "
            "pytest suite) + node suite dirs that exist.\n",
        )
        assert scan_docs([doc]) == []

    def test_test_skill_runner_doc_whitelisted(self, tmp_path: Path) -> None:
        doc = _write_doc(
            tmp_path, "skill/test/SKILL.md",
            "# Test skill\nRun `npx vitest run --project unit` here.\n"
            "```bash\npytest -q -r a --disable-warnings\n```\n",
        )
        assert scan_docs([doc]) == []

    def test_audit_f2_suite_resolution_prose_whitelisted(self, tmp_path: Path) -> None:
        doc = _write_doc(
            tmp_path, "skill/audit/SKILL.md",
            "**Per-project suite extension (F2, SA-0MSTMYE79006NA61):** "
            "resolution order: extension file > npm-test convention "
            "(`npm --silent test`) > pytest + node suite dirs.\n",
        )
        assert scan_docs([doc]) == []

    def test_resolve_pr_comments_snippet_whitelisted(self, tmp_path: Path) -> None:
        doc = _write_doc(
            tmp_path, "skill/resolve-pr-comments/SKILL.md",
            "never `npm --silent test` directly, which bypasses the per-repo "
            "test cache:\n",
        )
        assert scan_docs([doc]) == []

    def test_new_directive_in_resolve_pr_comments_still_flagged(self, tmp_path: Path) -> None:
        """The SA-0MSN6FBFF009VCOB whitelist covers exactly one snippet — a
        newly introduced direct directive in the same file fails the gate."""
        doc = _write_doc(
            tmp_path, "skill/resolve-pr-comments/SKILL.md",
            "Run `npm test` to verify PR-comment fixes.\n",
        )
        assert len(scan_docs([doc])) == 1

    def test_hook_and_ci_context_whitelisted(self, tmp_path: Path) -> None:
        doc = _write_doc(
            tmp_path, "docs/test-hook.md",
            "the pre-push hook re-runs the full suite (`--scope full`) on the "
            "actual push to dev.\n",
        )
        assert scan_docs([doc]) == []
