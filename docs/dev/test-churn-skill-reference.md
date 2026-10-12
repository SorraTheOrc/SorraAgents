# Test Churn Skill — Reference

> **Audience:** maintainers of the pi agent infrastructure and producers
> commissioning TDD-ROI evidence.
> **Skill:** [`skill/test-churn/SKILL.md`](../../skill/test-churn/SKILL.md)
> **Entry point:** `$(skill_path test-churn)/scripts/test_churn_analysis.py`

## Purpose

The test-churn skill quantifies how often test code is **rewritten** rather
than extended and turns that into reproducible TDD-ROI evidence for any git
repository. It is read-only against the analysed repository and writes two
artefacts: `test_churn_report.md` (human) and `test_churn_data.json`
(machine-readable).

## Usage

```bash
python3 "$(skill_path test-churn)/scripts/test_churn_analysis.py" \
    --project-root /path/to/repo \
    --branch dev \
    --output-dir /tmp/test-churn
```

The output directory defaults to the analysed project root. Use
`--format {md,json,both}`, `--since` and `--max-commits` to bound large
histories, and `--config <file>` to override the language-neutral
classification rules.

## Metric definitions

| Metric | Definition |
|---|---|
| **Rewrite** | A modified or renamed test file (status `M`/`R`) with both additions and deletions. |
| **New test file** | A test file added (status `A`). |
| **Deleted test file** | A test file removed wholesale (status `D`). |
| **Feature commit** | A commit that changes at least one source file. |
| **Rewrite ratio (modified only)** | `deletions / (deletions + additions)` over modified test files. Includes pure-addition and pure-deletion modifications in the denominator. |
| **Rewrite ratio (including new)** | The same numerator over the modified denominator **plus** additions from newly added test files. |
| **Temporal rewrite rate** | Distinct feature commits in a month with ≥1 rewrite event, divided by feature commits that month. Never exceeds 100%. |

Test and source classification is configurable through `ChurnConfig`; the
defaults are language-neutral and match common `test_*` / `*.test.ts` /
`tests/` conventions. No project-specific path is hardcoded.

## Corrected methodology

The analyser implements the corrections captured in the re-homed defect item
`SA-0MV2JE6MG003B0SW` (origin ContextHub `WL-0MV274I3R009XVZR`):

1. **Real status detection** — `git diff-tree --name-status` (A/M/D/R/T) with
   rename detection is joined to `--numstat`; statuses are never assumed.
2. **Documented denominators** — both rewrite-ratio variants are reported.
3. **Distinct-commit temporal rate** — counts commits, not rewrite events.
4. **Whole-file deletion accounting** — deletions are counted across the whole
   history, including test-only commits.
5. **Domain churn counts each file once.**

## Worked example

For the ContextHub repository (2,257 non-merge commits on `dev` at the time of
the port), the corrected analysis reported a modified-only rewrite ratio of
`11.8%` and an including-new ratio of `3.2%`; the monthly rate never exceeded
50%. For SorraAgents (1,661 commits) the corresponding figures were `13.7%`
and `5.9%`. The original ContextHub report overstated the ratio (21.1%) and
its temporal rate could exceed 100%; both defects are fixed here.

The exact figures depend on the configured test/source classification, so the
report always states the scope and the denominators used.

## Migration provenance

The capability was re-homed from ContextHub into SorraAgents as part of epic
`SA-0MV28WNFY009DF6Y`:

| Item | Detail |
|---|---|
| Source artefact | ContextHub `scripts/test_churn_analysis.py` at commit `141ceb8b`. |
| Source work item | ContextHub `WL-0MUZRNLFT004H8XJ` (superseded). |
| Defect work item | ContextHub `WL-0MV274I3R009XVZR` → re-homed as `SA-0MV2JE6MG003B0SW`. |
| Port work item | `SA-0MV28WROY007NAB4` (SorraAgents commit `ce121b8b`). |
| ContextHub removal | commit `b8e4eded` (script, data and report removed). |
| Skill work item | `SA-0MV28WS5G004YNNS`. |

Cross-worklog migration is manual: `wl` stores are per-repository, so the
relationship is recorded in descriptions and comments rather than dependency
edges. The full mapping, commit provenance and the carried-over original brief
are recorded in
[test-churn-worklog-migration.md](test-churn-worklog-migration.md).

## Related work

- [`skill/test-churn/SKILL.md`](../../skill/test-churn/SKILL.md) — the skill contract.
- [`skill/shared/test-writing-guidelines.md`](../../skill/shared/test-writing-guidelines.md) — the test anti-patterns whose detection motivated the TDD-value analysis.
- [`docs/dev/context-budget.md`](context-budget.md) — the startup-context budget the new skill description contributes to.
