---
name: mutation-test
description: Score how many injected source faults a test suite detects. Use when judging whether a large suite earns its runtime.
---

# Mutation Testing / Fault-Detection Analysis

Turn "this suite is large" into "this suite detects faults" by running
**Stryker Mutator** against the source modules behind a project's top-churn
test suites and reporting a mutation score per module and overall.

## Purpose

The mutation score answers a question a line count and a coverage percentage
cannot: **if the source were subtly wrong, would the suite notice?** Mutation
testing injects small faults (mutants) into production code and runs the
suite against each; a mutant that makes a test fail is *killed*, one that
survives exposes a behaviour the suite does not pin. The score
(`killed / (killed + survived + timeout + error)`, with surviving mutants
excluded from *equivalent* accounting where documented) is the honest
fault-detection measure behind TDD return on investment.

The skill is **read-only against the analysed repository** and requires no
per-project code changes to run.

## When to use

- A producer or engineering manager wants fault-detection evidence for a
  large or high-churn test suite, comparable over time.
- An agent needs a behavioural signal — stronger than churn or mock density —
  to decide which tests earn their runtime.
- You want to validate or falsify the hypothesis that **high-volume,
  mock-heavy suites detect fewer faults** than small, lean ones.

Do **not** use it as a whole-repo CI gate (see the runtime bound) and do not
treat a low score as proof of poor tests without reading the caveats below.

## Outputs

The wrapper writes two deterministic artefacts to the chosen output directory
(default: the project root):

- `mutation_testing_report.md` — self-contained human report: scope,
  suite→source mapping, per-module and overall mutation scores, mutant
  breakdown (killed / survived / timeout / error / no-coverage), runtime,
  budget-breach flag and the documented caveats.
- `mutation_report.json` — the same analysis, machine-readable, shaped for the
  test-value aggregator (`SA-0MVXVBZT7003VOCO`).

## Workflow

1. Resolve the skill directory at runtime; never assume a fixed repository
   layout. Ensure the pinned toolchain is installed (once per machine):

   ```bash
   ( cd "$(skill_path mutation-test)" && npm ci )
   ```

   `npm ci` installs the **pinned** Stryker pair recorded in
   `package-lock.json` into the skill's own `node_modules` — never into the
   analysed project.

2. Run the wrapper against the project root:

   ```bash
   python3 "$(skill_path mutation-test)/scripts/run_mutation.py" \
       --project-root /path/to/repo \
       --suite-map "$(skill_path mutation-test)/scripts/suite_source_map.json" \
       --output-dir /tmp/mutation-test
   ```

3. Read `mutation_testing_report.md`. The headline number is the overall
   mutation score; the per-module table shows which suites detect faults and
   which do not. A **budget breach** is reported explicitly with the partial
   result — never silently dropped.

4. Feed `mutation_report.json` to the test-value aggregator when a combined
   keep/rework/prune verdict is required.

### Options

| Option | Meaning |
|---|---|
| `project_root` (positional) / `--project-root` | Repository to analyse. |
| `--output-dir` | Where to write the two artefacts (default: project root). |
| `--suite-map` | Suite→source mapping (default: the skill's `scripts/suite_source_map.json`). |
| `--modules` | Comma-separated module ids to run (default: every module in the map). |
| `--per-module-budget-minutes` | Wall-clock budget per module (default: `15`). |
| `--total-budget-minutes` | Wall-clock budget for the whole run (default: `45`). |
| `--format {md,json,both}` | Which artefacts to write (default: `both`). |
| `--dry-run` | Print the merged Stryker config per module without running it. |
| `--stdout` | Also print the Markdown report. |

## Suite→source mapping

Mutation testing mutates **source**, but the churn ranking the `test-churn`
skill produces is over **test** files. The skill bridges the two with a documented
`suite_source_map.json` that names, for each top-churn suite, the source
module(s) it primarily exercises and the functions worth mutating:

```json
{
  "project": "ContextHub",
  "modules": [
    {
      "id": "downtime-worker",
      "suite": "packages/herdr/src/downtime-worker.test.ts",
      "source": "packages/herdr/src/downtime-worker.ts",
      "mutate": ["packages/herdr/src/downtime-worker.ts:120-240"]
    }
  ]
}
```

The mapping is a **documented assumption**, not machine-derived truth: it
records which source a suite is expected to cover so the score is
interpretable. Large modules (`worklist.ts` is thousands of lines) must be
scoped to the functions their tests exercise; the report states the scope so a
score is never read as whole-file coverage.

## Runtime bound and periodic runs

Fault detection is expensive, so the campaign is **bounded by policy**:

- **≤15 minutes per module**, **≤45 minutes per scoped run**.
- Cadence: **weekly or per-release**, scheduled on a **per-project day/time
  chosen to spread CPU load** across projects (no two campaigns contending for
  the same host slots).
- A run that **cannot complete within the bound** is reported as a **budget
  breach** with the partial result and the modules that did not finish —
  never silently degraded to no result.

Incremental mode (`.stryker-incremental.json` in the scratch working dir) keeps
repeat runs cheap; the first run of a large module pays the full cost.

## Caveats and limitations

A mutation score is evidence, not a verdict. The report states every caveat
that could mislead:

- **Sampling / scoping** — only the modules named by the suite→source map are
  mutated, and large modules are function-scoped. The score is about the
  sampled code, not the whole project.
- **Equivalent mutants** — some injected faults cannot change observable
  behaviour, so no correct test can kill them. They depress the score for
  reasons that are not weak testing; the report samples and flags survivors
  for manual review rather than treating every survivor as a gap.
- **Brittle kills** — a mutant can be killed because a test asserts an
  internal collaborator call rather than observable behaviour. Such kills
  inflate the score without proving behavioural coverage; record them
  separately with the assertion-quality signal
  (`SA-0MVXVC33N007Q71K`, `SA-0MV28WX8Y005B05G`).
- **Runtime cost** — mutation testing is not part of a project's normal CI
  suite; it runs under the bound above.
- **Tool pinning** — results are only comparable between runs of the same
  pinned Stryker version and the same suite→source map.

## Tool selection

**Stryker Mutator** with the **vitest runner** (`@stryker-mutator/core`,
`@stryker-mutator/api`, `@stryker-mutator/vitest-runner`), pinned to
**10.1.1** and licensed **Apache-2.0**. Rationale: the target projects are
TypeScript on vitest; Stryker has a first-class vitest runner, runs the
project's real test command, and emits a machine-readable JSON report the
wrapper parses. The exact versions are locked in `package-lock.json`; the base
config lives in `stryker.conf.json`. See
[docs/dev/mutation-testing.md](../../docs/dev/mutation-testing.md) for the
method, worked interpretation and periodic-run instructions.

## Definitions

- **Mutant** — a single injected source change (e.g. `<` → `<=`).
- **Killed** — a mutant that makes at least one test fail.
- **Survived** — a mutant no test detects (a gap, or an equivalent mutant).
- **Timeout** — a mutant that hangs the suite (treated as killed for the
  score, reported separately).
- **No coverage** — a mutant in code no test executes.
- **Mutation score** — `detected / (detected + survived)` where detected =
  killed + timeout; `error` and no-coverage mutants are reported separately.

## Reference

See [docs/dev/mutation-testing.md](../../docs/dev/mutation-testing.md) for the
method, interpretation guidance, caveats and the periodic-run schedule.

When inspecting Stryker artefacts, follow the shared
[tool-output budget](../shared/tool-output-budget.md) — `jq` over `cat`,
~10 KB per result.

## Tests

Unit tests live under `tests/` in this skill directory and cover the wrapper
(config merge, project-root resolution, argument parsing, budget logic) and the
report parser (score arithmetic, budget-breach detection) with Stryker mocked
(no real mutation run). Agent test execution must go through the test skill
(`/skill:test`) — the Test execution policy in `AGENTS_GLOBAL.md` prohibits
direct `pytest` invocations.
