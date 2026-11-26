# Skill-linter reference

Canonical contract for the `skill-linter` skill
(`skill/skill-linter/scripts/lint_skills.py`). The skill's `SKILL.md` is the
agent-facing operational brief; this document is the implementation reference —
the rule set, report schema, exit codes, `--apply` safety contract and cron
helper that the code and its tests cite. It is deliberately precise enough to
re-implement and re-test the linter from this page alone.

Work items: SA-0MWGAZMJX008CRLD (this reference, documentation deliverable of
parent **SA-0MWFP1E9P0027EOE**). The SKILL.md that links and summarises this
document is a separate child deliverable (SA-0MWGAZTX1008LZI4).

## 1. Purpose

The linter scans the repository's agent-instruction documents and reports two
families of problem:

1. **Duplicate content** — blocks of consecutive lines repeated inside one
   document or shared across documents.
2. **Traceability noise** — non-essential work-item IDs and workflow-comment
   lines (session logging, dispatch, claim release, phase chatter) that agents
   never act on.

Detection is **deterministic**: regular expressions and structural comparison
only. No LLM call gates, changes or waives a finding. The linter is
**read-only by default**; a scheduled (cron) run is always a dry run.

## 2. Invocation and CLI surface

```bash
python3 $(skill_path skill-linter)/scripts/lint_skills.py            # dry run (default)
python3 $(skill_path skill-linter)/scripts/lint_skills.py --json     # machine-readable report
python3 $(skill_path skill-linter)/scripts/lint_skills.py --apply    # apply safe fixes only
python3 $(skill_path skill-linter)/scripts/lint_skills.py --install-cron weekly
```

| Flag | Default | Effect |
|---|---|---|
| `--root PATH` | `.` | Repository root the default scope and relative targets resolve against; echoed (resolved, absolute) as the report's `root`. |
| `--target PATH` | none | Scan one file or directory (repeatable). **Replaces** the default scope entirely — see §3. |
| `--apply` | off | Apply `FIX_SAFE` findings; recommendations are still only reported (§8). |
| `--dry-run` | **on** | Report what would change and modify nothing. Wins over `--apply`: `--apply --dry-run` is a dry run. |
| `--json` | off | Emit the JSON report (§6.2) instead of the text report (§6.1). |
| `--install-cron [SCHEDULE]` | `weekly` when the schedule is omitted | Print a crontab line for a scheduled dry-run report; **prints only, never installs** (§9). |
| `--allow-id ID` | none (empty allow-list) | Work-item ID that is essential: preserved by detection and stripping. Repeatable. |
| `--min-lines N` | `MIN_DUPLICATE_LINES` = `3` | Minimum consecutive non-blank lines in a duplicate block; `N < 2` is a usage error. |
| `--min-chars N` | `MIN_DUPLICATE_CHARS` = `80` | Minimum characters in a duplicate block; negative values are usage errors. |
| `--help` | — | Usage summary. |

## 3. Scan scope

**Default scope** (`DEFAULT_SKILL_GLOB` = `skill/*/SKILL.md`, plus
`DEFAULT_ROOT_DOCUMENTS` = `AGENTS.md` and `AGENTS_GLOBAL.md` at the repo root):

- every `skill/<name>/SKILL.md`, one level deep, and
- `AGENTS.md` and `AGENTS_GLOBAL.md` at the root.

Nothing else is scanned by default — not `skill/<name>/README.md`, not nested
`AGENTS.md` files, not `command/`, not `docs/`. Missing root documents are
simply absent (no error). Hidden directories are never traversed. This matches
the deployment model: `~/.pi/agent/skills` is a symlink to this repository's
`skill/`, so a repo-local scan covers the installed skills.

**`--target` override.** Any `--target` replaces the default scope completely:

- a **file** target is read as-is, even outside `--root`;
- a **directory** target expands recursively over the document names in
  `SCAN_DOCUMENT_NAMES` = `SKILL.md`, `AGENTS.md`, `AGENTS_GLOBAL.md` (hidden
  directories skipped);
- repeated `--target` values are scanned together and overlapping targets read
  each document once;
- relative targets join `--root`; a missing target is a usage error (exit `2`);
- a target that is not valid UTF-8 is a scan error (exit `2`) with an
  actionable message — convert it or exclude it.

Report paths are root-relative POSIX paths (absolute when the target lies
outside `--root`).

## 4. Detection rules

Findings are ordered deterministically by first span: `(path, start_line,
kind)`. Re-running the same scan over the same tree produces byte-identical
reports.

### 4.1 Exact duplicate blocks (`duplicate`, `FIX_SAFE`)

Constants: `MIN_DUPLICATE_LINES` = `3`, `MIN_DUPLICATE_CHARS` = `80`.

Algorithm:

1. Split every scanned document into lines; keep the original lines for
   reporting.
2. Slide a window of `MIN_DUPLICATE_LINES` consecutive lines over each
   document. **Any window containing a blank (whitespace-only) line is
   skipped** — blank lines bound a block and are never part of a duplicate.
3. Normalise each window to a key of per-line `str.strip()` values, i.e.
   leading and trailing whitespace is ignored when comparing; internal spacing
   and letter case are **not** normalised, and blank-line-free windows only.
4. Windows whose key occurs once cannot be a duplicate and are discarded.
5. Extend each shared run to the **longest** run common to all its occurrences:
   keep adding the next line while it is non-blank in every occurrence and
   identical after normalisation.
6. Claim the longest runs first; a start already claimed by an earlier run, or
   overlapping an already-consumed line range in the same file, is skipped.
   This prevents a short prefix from masking the longer duplicate containing
   it and stops a line being reported twice.
7. A run is reported only when at least two live occurrences remain **and** the
   first span's original text is at least `MIN_DUPLICATE_CHARS` characters.
8. Each duplicated run yields **one** finding: `kind = "duplicate"`,
   `fix_class = "safe"`, `text` = the first occurrence's text, and one `span`
   per live occurrence (root-relative path, 1-based `start_line`/`end_line`,
   and the original, unmodified lines).

Because comparison strips indentation but reporting does not, a block
indented differently in two places is still a duplicate and the report shows
each original indentation.

Worked example: the same three-line block appears at lines 5–7 and 12–14 of
`skill/alpha/SKILL.md` → one finding whose `spans` name `5–7` and `12–14`. It
is ≥ 80 characters and ≥ 3 lines, so it is reported.

### 4.2 Near-duplicate blocks (`near_duplicate`, `FIX_RECOMMENDATION`)

Similar-but-not-identical blocks need human judgement, so they are
**recommendations**, never auto-applied. The CLI always enables this detector
(`include_near=True`); the library default (`ScanConfig.include_near`) is
`False`.

- Candidate windows are the same `MIN_DUPLICATE_LINES`-line, blank-free windows
  of at least `MIN_DUPLICATE_CHARS` characters, excluding any line already
  covered by an exact duplicate.
- Only window pairs sharing at least one identical normalised line are
  compared (an inverted line index keeps this tractable on real trees).
  Identical windows (already reported as exact duplicates) and same-file
  window pairs whose starts differ by less than `MIN_DUPLICATE_LINES`
  (overlapping) are skipped.
- Similarity is `difflib.SequenceMatcher` ratio over the raw window text. At or
  above `NEAR_DUPLICATE_RATIO` = `0.85` the pair becomes one finding:
  `kind = "near_duplicate"`, `fix_class = "recommendation"`, two spans of
  exactly `MIN_DUPLICATE_LINES` lines each, and a rationale quoting the measured
  ratio.
- Near-duplicate spans are not extended and the report never proposes a merged
  text — the operator decides which copy survives or how to shorten it.

### 4.3 Non-essential work-item IDs (`nonessential_id`, `FIX_SAFE`)

`WORK_ITEM_ID_RE` = `\b[A-Z]{2,6}-\d[0-9A-Z]{8,}\b`: an uppercase prefix of
2–6 letters, a hyphen, then a digit followed by at least 8 ASCII alphanumerics
(e.g. `SA-0MWGAYPMX002Z0VC`, `WL-0MSS55LFU00973S2`).

- One finding per matching ID per line. The entry's `text` is the **ID token**;
  the span covers the **whole line** it sits on (`start_line == end_line`), so
  the report shows enough context to judge the removal.
- **Allow-list.** `DEFAULT_ALLOW_IDS` is empty: by default **no work-item ID is
  essential**, so every ID is removable traceability noise. `--allow-id` adds
  IDs that must be preserved. Matching is exact and case-sensitive; plausible
  IDs are never inferred automatically.
- **Stripping** (`--apply`, and every finding's `proposed_text`): each
  non-allow-listed ID is removed from the line, then the line is tidied —
  empty bracket pairs (`()`, `[]`, `<>`, `{}`) left behind by the removal are
  deleted, whitespace before punctuation is closed up, runs of spaces are
  collapsed, and trailing whitespace is dropped.
- A line that held **nothing but traceability** — e.g. `- (SA-0…)`, `1. SA-0…`,
  `> SA-0…`, or a table cell `| SA-0… |` — is dropped whole rather than left as
  an empty bullet, list number, quote marker or table row. "Nothing but
  traceability" means that after removing Markdown structure (list bullets
  `-`/`*`/`+`, ordered-list numbers `1.`/`1)`, task markers `[ ]`/`[x]`,
  blockquote markers `>`) the residual line carries no alphanumeric character.
- Trailing-newline presence is preserved when stripping.

### 4.4 Workflow-comment noise (`workflow_comment`, `FIX_SAFE`)

`WORKFLOW_COMMENT_RULES` matches whole lines; one finding is emitted per
matching line, with `text` and its span set to the whole line. Rule order only
affects the label used in messages (first match wins):

| Label | Matches |
|---|---|
| `session-logging line` | A line containing `Session ID:` followed by a real session identifier (a UUID, or a `herdr-<n>-<n>…` id) and ` - <path>`, e.g. `implement - Session ID: 01a2…-… - /home/operator/.pi/agent/sessions/session.jsonl`. A line that merely documents the placeholder (`Session ID: <pi_session_id>`) does **not** match. |
| `dispatch line` | Line starts with `Auto-dispatched by` (case-insensitive). |
| `claim-release line` | Line starts with `Claim released:`. |
| `implement status line` | Line starts with `implement started` or `implement complete` (case-insensitive). |
| `phase status line` | Line starts with `Start phase:` or `Finish phase:`. |
| `completion marker` | The whole line is exactly `Work committed to dev`. |

Every rule describes a line that carries traceability, not an instruction, so
removal cannot lose meaning; the rationale is fixed to
`workflow-traceability comment line, not instruction`.

## 5. Fix-class taxonomy and permission flow

| kind | `fix_class` | Auto-applied by `--apply`? | Action |
|---|---|---|---|
| `duplicate` | `safe` | **Yes** | Remove every copy after the first occurrence. |
| `nonessential_id` | `safe` | **Yes** | Strip the ID (and a traceability-only line) from the document. |
| `workflow_comment` | `safe` | **Yes** | Remove the workflow-comment line. |
| `near_duplicate` | `recommendation` | **No** | Report only; merging or shortening needs operator judgement. |

`requires_approval` is `true` exactly for `fix_class == "recommendation"`
(`FIX_RECOMMENDATION`). The permission flow:

1. Run the default **dry run**; nothing is written.
2. Review each finding: file, section, line span, current text, proposed text,
   action and rationale.
3. **Safe class:** approve by re-running with `--apply` (safe fixes only), or
   make the same edit by hand. Commit the result.
4. **Recommendations** (content that could be removed but is not clearly safe,
   and content that could be rewritten for brevity without losing clarity):
   never modified automatically: applying a recommendation requires explicit
   operator approval. The operator reviews the context, decides, and edits the
   document — an approval is per finding and explicit. There is no flag that
   applies recommendations.
5. Re-run and confirm the report converges (`EXIT_CLEAN`), then review the diff
   in git. The weekly scheduled run is a dry run, so the schedule can never
   change a file.

## 6. Report schema

### 6.1 Text report

The text report is a stable human-readable projection of the same payload:

```
skill-linter report (dry-run)
root: /abs/path/to/repo
(dry-run: no files modified)

<path>:<start>-<end>  <kind>  [fix class: <safe|recommendation>]  section: <heading>
  current text:
    <each line of the finding text, indented four spaces>
  proposed text:
    <each line of proposed_text, or>
    (line removed)
  action: <what a fix would do>
  rationale: <why it was reported>
  approval: required — operator must approve; never auto-applied   # recommendations only

summary: <total> finding(s) — <safe> safe, <recommendation> recommendation, <applied> applied, <remaining> remaining
```

`(dry-run: no files modified)` appears only for a dry run; an empty report
prints `no findings — clean`. An applied run's header reads
`skill-linter report (apply)` and reports the post-apply state.

### 6.2 JSON report

`schema_version` = `REPORT_SCHEMA_VERSION` = `1`; it is bumped only for an
incompatible shape change (adding a field is compatible). Payload:

| Key | Type | Meaning |
|---|---|---|
| `schema_version` | int | Report schema version (`1`). |
| `root` | string | Resolved absolute root the scan ran against. |
| `dry_run` | bool | `true` when no file was written; `false` after an applied run. |
| `findings` | array | One entry per finding, in deterministic order. |
| `summary` | object | Counts — `total`, `safe`, `recommendation`, `applied`, `remaining`. |

Finding entry:

| Key | Type | Meaning |
|---|---|---|
| `kind` | string | `duplicate`, `near_duplicate`, `nonessential_id` or `workflow_comment`. |
| `fix_class` | string | `safe` (`FIX_SAFE`) or `recommendation` (`FIX_RECOMMENDATION`). |
| `path` | string | Root-relative POSIX path of the first span. |
| `start_line`, `end_line` | int | 1-based inclusive line span of the first span (equal for single-line findings). |
| `section` | string | Nearest Markdown heading at or above `start_line`, verbatim (`## Usage`), or `(document top)` when none precedes the line. |
| `text` | string | The finding text: the duplicated block (first occurrence), the ID token, or the workflow-comment line. Embedded newlines for multi-line findings. |
| `proposed_text` | string | What a fix would leave: unchanged block for duplicates and near-duplicates; the tidied whole line for an ID; the empty string for a workflow comment (rendered `(line removed)`). |
| `rationale` | string | Why the finding was reported (see §4; near-duplicates quote their similarity ratio). |
| `requires_approval` | bool | `true` exactly when `fix_class` is `recommendation`. |
| `action` | string | Human-readable remedy, e.g. `would remove duplicate copies, keeping the first occurrence`. |
| `spans` | array | Every instance, each `{path, start_line, end_line, text}` with the **original** lines in `text`. For `nonessential_id`, the span text is the whole line while the entry `text` is the ID token. |

`summary` semantics: `total` = number of findings in this report (the report
always reflects the tree *after* the run, so an applied run re-scans);
`safe`/`recommendation` count each class; `applied` is the number of
`FIX_SAFE` findings the run removed (`0` on a dry run); `remaining` is the
number of findings still present after the run (equal to `total`; retained so
callers need not infer it).

Example payload for one duplicate plus one ID:

```json
{
  "schema_version": 1,
  "root": "/home/operator/projects/SorraAgents",
  "dry_run": true,
  "findings": [
    {
      "kind": "duplicate",
      "fix_class": "safe",
      "path": "skill/alpha/SKILL.md",
      "start_line": 5,
      "end_line": 7,
      "section": "## Usage",
      "text": "The linter scans SKILL.md files for repeated blocks of prose.\nIt normalises whitespace before comparing candidate windows.\nEvery finding reports the file path and the exact line span.",
      "proposed_text": "The linter scans SKILL.md files for repeated blocks of prose.\nIt normalises whitespace before comparing candidate windows.\nEvery finding reports the file path and the exact line span.",
      "rationale": "exact duplicate block (identical after whitespace normalisation) — removing extra copies cannot lose meaning",
      "requires_approval": false,
      "action": "would remove duplicate copies, keeping the first occurrence",
      "spans": [
        {
          "path": "skill/alpha/SKILL.md",
          "start_line": 5,
          "end_line": 7,
          "text": "The linter scans SKILL.md files for repeated blocks of prose.\nIt normalises whitespace before comparing candidate windows.\nEvery finding reports the file path and the exact line span."
        },
        {
          "path": "skill/alpha/SKILL.md",
          "start_line": 12,
          "end_line": 14,
          "text": "The linter scans SKILL.md files for repeated blocks of prose.\nIt normalises whitespace before comparing candidate windows.\nEvery finding reports the file path and the exact line span."
        }
      ]
    },
    {
      "kind": "nonessential_id",
      "fix_class": "safe",
      "path": "skill/alpha/SKILL.md",
      "start_line": 9,
      "end_line": 9,
      "section": "## Usage",
      "text": "SA-0MWGAYPMX002Z0VC",
      "proposed_text": "Tracking note for.",
      "rationale": "work-item ID is workflow traceability, not an instruction; allow-list it to keep it",
      "requires_approval": false,
      "action": "would strip work-item ID SA-0MWGAYPMX002Z0VC",
      "spans": [
        {
          "path": "skill/alpha/SKILL.md",
          "start_line": 9,
          "end_line": 9,
          "text": "Tracking note for SA-0MWGAYPMX002Z0VC."
        }
      ]
    }
  ],
  "summary": {
    "total": 2,
    "safe": 2,
    "recommendation": 0,
    "applied": 0,
    "remaining": 2
  }
}
```

## 7. Exit codes

| Constant | Code | Meaning |
|---|---|---|
| `EXIT_CLEAN` | `0` | No findings remain (a clean tree, or `--apply` cleared every safe finding). |
| `EXIT_FINDINGS` | `1` | One or more findings are still reported. |
| `EXIT_ERROR` | `2` | Usage or scan error (bad threshold, missing/invalid target, non-UTF-8 file, unknown cron schedule). |

`--install-cron` success exits `0` on its own path (`EXIT_CRON_OK`) regardless
of what a scan would find — "cron line printed" is not "tree clean". Errors are
written to stderr prefixed `lint_skills: error:` with an actionable remedy.

## 8. `--apply` safety contract

- **Read-only and dry-run by default.** Only an explicit `--apply` writes; `--dry-run`
  overrides `--apply`, so the default and the scheduled run can never modify a
  file.
- **Safe-fix classes only.** `--apply` writes only `FIX_SAFE` findings: exact
  duplicate copies after the first, non-allow-listed work-item IDs (and
  traceability-only lines), and workflow-comment lines. `FIX_RECOMMENDATION`
  findings (near-duplicates) are reported and never written.
- **Allow-list respected.** `--allow-id` IDs survive both detection and
  stripping.
- **Only changed files are rewritten**, in place as UTF-8. No backups, temp
  copies or index files are created — git is the undo mechanism.
- **The report describes the result.** After writing, the tree is re-scanned;
  the report carries `dry_run: false`, the `applied` count and the remaining
  findings, and the exit code reflects what is left (`0` when clean, `1` when
  only recommendations remain).
- **No surface growth.** Applying safe fixes may only shrink the measured
  context surface; a repo-level test asserts no measured component grows.

## 9. Weekly scheduling (`--install-cron`)

`--install-cron` takes an optional schedule name; the only supported name is
`weekly` (also the default when the flag is given without a value). `weekly`
means `CRON_SCHEDULE_WEEKLY` = `0 3 * * 1` — every Monday at 03:00 in the
machine's local cron time.

The command **prints** the crontab line and exits; it never calls `crontab`,
never edits the crontab, and must not claim the schedule is active:

```
Weekly skill-linter dry-run crontab line (printed only; NOT installed — add it with `crontab -e`):

0 3 * * 1 cd /abs/path/to/repo && /usr/bin/python3 /abs/path/to/skill/skill-linter/scripts/lint_skills.py --dry-run --json >> /abs/path/to/repo/.worklog/skill-linter-cron.log 2>&1
```

- The root, interpreter (`sys.executable`) and script paths are resolved to
  absolute paths and shell-quoted with `shlex.quote` when needed.
- The scheduled run passes `--dry-run --json`, so each run appends one JSON
  report to `<root>/.worklog/skill-linter-cron.log` and changes nothing.
- The library helper is `cron_line(root, script, log_path=None)`; passing
  `log_path` overrides the default `.worklog/skill-linter-cron.log` location.
- Installation is the operator's step: add the printed line with
  `crontab -e`, save, then confirm with `crontab -l`. Until that is done the
  schedule is **not active** and neither the skill nor its report may say
  otherwise.
- An unknown schedule name is a usage error (exit `2`) naming the supported
  names.

## 10. Library API

`skill/skill-linter/scripts/lint_skills.py` is importable as `lint_skills`
(tests add `scripts/` to `sys.path`):

- `ScanConfig(min_lines, min_chars, include_near, near_ratio, allow_ids)` —
  frozen dataclass; validates `min_lines >= 2`, `min_chars >= 0`,
  `near_ratio` in `(0, 1]`, and `allow_ids` as an iterable of strings.
  `ScanConfig().scan(documents)` applies the configuration.
- `scan_documents(documents, *, allow_ids, include_near, min_lines, min_chars,
  near_ratio)` — run every detector over `{path: text}`.
- `discover_documents(root, *, targets=None)` — read the scope (§3).
- `scan_paths(root, *, targets=None, config=None)` — discovery + scanning, the
  entry point the CLI wraps.
- `find_duplicates(...)` raises `ValueError` when `min_lines < 2` (a shorter
  window cannot distinguish a duplicate from boilerplate).
- `apply_safe_fixes(documents, findings, *, allow_ids)` returns
  `{path: cleaned_text}` for changed documents and writes nothing — the CLI is
  the only writer.
- `build_report(...)`, `render_text_report(report)`,
  `render_json_report(report)` — reporting (§6).
- `cron_line(...)` and `main(argv)` — cron helper (§9) and the CLI.

## 11. Contract ownership and verification

The reference, the code and the tests ship together. The rules above are
pinned by:

- `skill/skill-linter/tests/test_lint_skills.py` — duplicate/near-duplicate
  detection, ID and workflow-comment detection and stripping, fix
  classification, text/JSON report contract, `--dry-run`, `--apply`,
  `--target`, `--install-cron` and exit codes.
- `skill/skill-linter/tests/test_scan_scope.py` — default scope, `--target`
  expansion, `ScanConfig` defaults/validation and `scan_paths`.
- `skill/skill-linter/tests/test_reference_docs.py` — doc drift: every
  threshold, exit code, fix class, finding kind, workflow rule label, CLI flag
  and JSON report key the code exposes must appear in this document, and the
  README must link it.

The skill's `SKILL.md` links this reference and does not duplicate its rule
tables; that link is delivered by the SKILL.md child item
(SA-0MWGAZTX1008LZI4), since the two documents are separate deliverables of the
same parent.

**Changing the contract.** Update the implementation, its tests and this
document in the same change; the doc-drift test fails until the new rule, flag
or field is documented. Bump `REPORT_SCHEMA_VERSION` only when an existing JSON
consumer would break (a removed or retyped field); additive changes keep the
version.

## 12. Context budget

This reference is on-demand documentation: it is not part of the pi startup
context measured by `docs/dev/context-budget.thresholds.json` (which measures
`AGENTS_GLOBAL.md`, the project `AGENTS.md`, skill frontmatter descriptions and
the implement skill body), so adding it does not consume that gate's budget.
The skill's own `SKILL.md` is measured only through its frontmatter description
(`skills_prose`). The linter's `--apply` path is constrained never to grow a
measured component (§8).
