# Mutation Testing — Reference

> **Audience:** maintainers of the pi agent infrastructure and producers
> commissioning fault-detection evidence for large test suites.
> **Skill:** [`skill/mutation-test/SKILL.md`](../../skill/mutation-test/SKILL.md)
> **Entry point:** `$(skill_path mutation-test)/scripts/run_mutation.py`

## Purpose

The mutation-test skill measures **real test value**: it injects small faults
(mutants) into source modules and runs the project's test suite against each.
A mutant that makes a test fail is *killed*; one that survives exposes a
behaviour the suite does not pin. The mutation score turns "this suite is
large" into "this suite detects faults", the behavioural measure behind TDD
return on investment.

The skill is **read-only against the analysed repository** and requires no
per-project code changes to run.

## Tool selection and pinning

| Field | Value |
|---|---|
| Tool | Stryker Mutator |
| Packages | `@stryker-mutator/core`, `@stryker-mutator/api`, `@stryker-mutator/vitest-runner` |
| Version | `10.1.1` (pinned in `skill/mutation-test/package-lock.json`) |
| Licence | Apache-2.0 |
| Runner | `vitest` (native Stryker runner) |

**Why Stryker.** The analysed projects are TypeScript on vitest. Stryker has a
first-class vitest runner, executes the project's real test command, and emits
a machine-readable JSON report the wrapper parses. The pinned version is
recorded in the skill's own `package.json` + `package-lock.json`; results are
only comparable between runs of the same pinned version and the same
suite→source map.

Install the toolchain once per machine (into the skill directory, never the
analysed project):

```bash
( cd "$(skill_path mutation-test)" && npm ci )
```

## Usage

```bash
python3 "$(skill_path mutation-test)/scripts/run_mutation.py" \
    --project-root /path/to/repo \
    --suite-map "$(skill_path mutation-test)/scripts/suite_source_map.json" \
    --output-dir /tmp/mutation-test
```

The output directory defaults to the analysed project root. Use `--modules` to
run a subset, `--dry-run` to print the merged Stryker config per module without
executing it, and `--format {md,json,both}` to select artefacts.

## Suite→source mapping

Mutation testing mutates **source**, but `skill/test-churn` ranks **test**
files. The skill bridges the two with a documented `suite_source_map.json`
recording, for each top-churn suite, the source module(s) it primarily
exercises and the functions worth mutating. The mapping is a documented
assumption so the score is interpretable; large modules (`worklist.ts` is
thousands of lines) are function-scoped, and the report states the scope so a
score is never read as whole-file coverage.

## Metric definitions

| Term | Definition |
|---|---|
| **Mutant** | A single injected source change (e.g. `<` → `<=`). |
| **Killed** | A mutant that makes at least one test fail. |
| **Survived** | A mutant no test detects (a gap, or an equivalent mutant). |
| **Timeout** | A mutant that hangs the suite; counted as detected for the score, reported separately. |
| **Error** | A mutant the runner could not evaluate; reported separately, excluded from the score. |
| **No coverage** | A mutant in code no test executes; reported separately. |
| **Mutation score** | `detected / (detected + survived)`, where detected = killed + timeout. |

## Runtime bound and budget breach

Fault detection is expensive, so the campaign is **bounded by policy**:

- **≤15 minutes per module**, **≤45 minutes per scoped run**.
- Incremental mode keeps repeat runs cheap; the first run of a large module
  pays the full cost.

A run that **cannot complete within the bound** is reported as a **budget
breach** carrying the partial result and the modules that did not finish. It is
never silently degraded to no result — a caller can always distinguish "no
faults survived" from "the campaign ran out of time".

## Periodic runs and scheduling

The campaign is designed for a **weekly or per-release** cadence, scheduled on
a **per-project day/time chosen to spread CPU load** across projects so no two
campaigns contend for the same host slots. The scheduled command is the
`run_mutation.py` invocation above, with a stable `--output-dir` so successive
runs are comparable.

## Caveats

A mutation score is evidence, not a verdict. Every run states:

- **Sampling / scoping** — only the modules named by the suite→source map are
  mutated, and large modules are function-scoped. The score is about the
  sampled code, not the whole project.
- **Equivalent mutants** — some injected faults cannot change observable
  behaviour, so no correct test can kill them. They depress the score for
  reasons that are not weak testing; survivors are sampled and flagged for
  manual review rather than treated as proof of a gap.
- **Brittle kills** — a mutant can be killed because a test asserts an internal
  collaborator call rather than observable behaviour. Such kills inflate the
  score without proving behavioural coverage; record them separately with the
  assertion-quality signal (`SA-0MVXVC33N007Q71K`, `SA-0MV28WX8Y005B05G`).
- **Runtime cost** — mutation testing is not part of a project's normal CI
  suite; it runs under the bound above.
- **Tool pinning** — results are only comparable between runs of the same
  pinned Stryker version and the same suite→source map.

## Related work

- `SA-0MV28WY5Q003N54O` — this campaign (top-churn modules).
- `SA-0MVSGIMHK004QHOO` — test-value refactor epic (mutation testing is its
  evidence arm).
- `SA-0MV28WNFY009DF6Y` — test-churn migration (supplies the top-churn input).
- `SA-0MVXVBZT7003VOCO` — test-value aggregator (consumes
  `mutation_report.json`).
