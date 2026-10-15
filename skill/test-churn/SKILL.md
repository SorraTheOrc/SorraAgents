---
name: test-churn
description: Analyse git history to quantify test-code rewrites and TDD value. Use when assessing test churn, test growth, or TDD ROI.
---

# Test Churn / TDD-Value Analysis

Quantify how often test code is **rewritten** rather than extended, and turn
that into reproducible TDD-ROI evidence for any git repository.

## Purpose

The skill answers a single question with evidence: do the tests in this
project survive feature evolution, or are they rewritten almost as often as
they are written? It reads a project's git history, classifies every test-file
change (added / modified / deleted / renamed), and reports a documented
rewrite ratio plus supporting churn metrics. It is read-only against the
analysed repository and never requires per-project code changes.

## When to use

- A producer or engineering manager wants reproducible TDD-ROI / test-growth
  evidence for a project, comparable over time.
- An agent working on testing policy needs honest churn numbers instead of
  anecdote.
- You suspect tests are coupled to implementation details and want to measure
  how much test code is rewritten.

Do **not** use it to prove that tests were written before their source — git
cannot show that. The report labels its co-evolution metric accordingly.

## Outputs

The entry point writes two deterministic artefacts to the chosen output
directory (default: the project root):

- `test_churn_report.md` — self-contained human report: scope, definitions,
  limitations, corrected metrics and a TDD-value assessment.
- `test_churn_data.json` — the same analysis, machine-readable.

## Workflow

1. Run the analyser against the project root. Resolve the skill directory at
   runtime; never assume a fixed repository layout:

   ```bash
   python3 "$(skill_path test-churn)/scripts/test_churn_analysis.py" \
       --project-root /path/to/repo \
       --output-dir /tmp/test-churn
   ```

2. Read `test_churn_report.md`. The two headline numbers are the rewrite ratio
   over **modified test files only** and the variant **including new test
   files**; both denominators are documented in the report.
3. Use `test_churn_data.json` when the numbers must feed another tool or a
   time series.

### Options

| Option | Meaning |
|---|---|
| `project_root` (positional) / `--project-root` | Repository to analyse. |
| `--branch` | Mainline branch (default: `dev`, then `main`, `master`, `HEAD`). |
| `--output-dir` | Where to write the two artefacts (default: project root). |
| `--format {md,json,both}` | Which artefacts to write (default: `both`). |
| `--since`, `--max-commits` | Bound a large history (performance). |
| `--config` | JSON file overriding test/source classification rules. |
| `--stdout` | Also print the Markdown report. |

### Corrected metrics

The analyser implements the corrected methodology re-homed from ContextHub
(`SA-0MV2JE6MG003B0SW`):

1. Real git `--name-status` (A/M/D/R/T) with rename detection — never an
   assumed status.
2. Two documented rewrite-ratio denominators: modified test files only, and
   including newly added test files.
3. Temporal trends count **distinct** feature commits with at least one
   rewrite, so a monthly rate can never exceed 100%.
4. Whole-file test deletions are accounted for across the whole history.
5. Domain churn counts each file exactly once.
6. A consolidated **test-growth metric** — tests added / deleted / modified
   and **net growth** — is reported per project and per test area, and is
   trended **per calendar quarter** from commit dates (empty quarters report
   zero, they are never omitted).

## Test growth metric

Section 3 of the report (and the `growth_metrics` block of the JSON) gives an
honest, at-a-glance growth figure: how many test files and lines were **added**,
**deleted** and **modified** across the whole analysed history, and the **net
growth** (`added − deleted`). File counts count *change events*; line counts
accumulate the real name-status + numstat figures. The accounting spans **all**
commits (not just feature commits), so a whole-file deletion or a new test file
in a test-only commit is never invisible. Section 4 trends the same figures per
calendar quarter (`YYYY-QN`, derived from commit dates), making growth
regressions over time visible.

Pruning a suite always starts with this section: a positive net growth quarter
over quarter, or areas where additions far outstrip deletions, are the signal
that the suite may be growing without bound. See the pruning workflow below.

## Pruning review workflow

When, on this metric, a suite is growing without manifest benefit, prune tests
that **no longer map to observable behaviour** using the following repeatable
workflow. The criteria come from
[`../shared/test-writing-guidelines.md`](../shared/test-writing-guidelines.md):

1. **Locate the signal.** Run the analyser and read the growth section: net
   growth by area and the quarterly trend. Note the areas and files flagged by
   the report (top volatile files, high-rewrite files).
2. **Inspect candidates.** For each candidate test file, apply the anti-pattern
   checklist from the test-writing guidelines:
   - source-code-grep tests (assert file text, not behaviour);
   - placeholder tests (`expect(true).toBe(true)`, zero assertions);
   - self-referential simulations (the test re-implements the production
     logic it claims to test);
   - duplicates of existing core coverage (same behaviour covered via a
     different import path);
   - type-level / structural-only tests (asserting what the compiler already
     checks);
   - zero-assertion browser/scene tests.
3. **Verify against observable behaviour.** For each candidate, confirm the
   test no longer exercises a public behaviour that is exercised elsewhere:
   `git blame` the file to see when it last asserted something, and grep the
   suite for equivalent coverage of the same behaviour through the public
   API. If the behaviour it pinned is still covered by a live test, the
   candidate is prunable.
4. **Prune.** Remove the dead test (or file), run the full suite and confirm
   nothing breaks. Record the removal in a Worklog item — the pruning pass is
   a tracked change, not an ad-hoc edit.

**Pruning criteria are conservative:** when in doubt, keep the test and file a
work item instead. Removing a useful test is a regression; keeping a dead one
only costs runtime. Pruning is deliberately higher-risk than measuring, so it
is tracked as a follow-on work item rather than bundled into a metrics change.

## Definitions

- **Rewrite** — a modified/renamed test file with both additions and
  deletions (existing test lines replaced).
- **New test file** — a test file added (git status `A`).
- **Deleted test file** — a test file removed wholesale (git status `D`).
- **Net growth** — files/lines of tests added minus files/lines deleted over
  the whole analysed history; file counts count change events.
- **Calendar quarter** — a `YYYY-QN` grouping derived from a commit's date.
- **Feature commit** — a commit that changes at least one source file.

## Limitations

- Branch-scoped: only the selected mainline is analysed; other branches and
  uncommitted work are ignored.
- Git cannot prove test-first ordering; the co-evolution metric is descriptive.
- Test/source classification uses configurable, language-neutral defaults;
  pass `--config` to match unusual layouts.
- A rewrite can reflect a legitimate interface change, not only poor test
  design.
- Growth file counts count change events (a delete-and-re-add of the same path
  is one deletion and one addition), and are bounded by rename-detection
  heuristics.

## Configuration

`--config` accepts a JSON object merging over `ChurnConfig` defaults:
`test_filename_patterns`, `test_dir_segments`, `code_extensions`,
`excluded_segments` and `area_rules`. No project-specific paths are built in.

## Reference

See [docs/dev/test-churn-skill-reference.md](../../docs/dev/test-churn-skill-reference.md)
for the metric definitions, migration provenance and worked examples.

## Tests

Unit and end-to-end tests live under `tests/` in this skill directory.
Agent test execution must go through the test skill (`/skill:test`) —
the Test execution policy in AGENTS_GLOBAL.md prohibits direct
`pytest` invocations.
