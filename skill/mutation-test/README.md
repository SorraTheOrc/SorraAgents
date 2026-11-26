# mutation-test

Measure real test value by injecting faults into source modules and scoring
how many the suite detects.

This directory holds a pinned **Stryker Mutator** toolchain
(`@stryker-mutator/core`, `@stryker-mutator/api`,
`@stryker-mutator/vitest-runner` — all `10.1.1`, Apache-2.0) and a Python
wrapper that runs the campaign read-only against any TypeScript/vitest project.

- **Start here:** [SKILL.md](SKILL.md) — purpose, workflow, options, outputs,
  the runtime bound and the caveats.
- **Method and interpretation:**
  [docs/dev/mutation-testing.md](../../docs/dev/mutation-testing.md).
- **Tool pinning:** `package.json` + `package-lock.json`.
- **Base config:** `stryker.conf.json` (scope is supplied per module by the
  wrapper, so it is not hard-coded here).

Install the pinned toolchain once per machine (into this directory, never the
analysed project):

```bash
( cd "$(skill_path mutation-test)" && npm ci )
```

Then run the campaign against a project root:

```bash
python3 "$(skill_path mutation-test)/scripts/run_mutation.py" \
    --project-root /path/to/repo \
    --output-dir /tmp/mutation-test
```
