# node/vitest changed-scope selection order

> **Work item:** SA-0MW0LNT970057DM9 — *implement finish changed-scope gate:
> prove scoped selection, document selection order, measure wall clock
> (AI_Hell case study)*. This page documents the selection order the
> changed-scope gate applies to node/vitest repos. It is deliberately short
> and pointer-heavy so the canonical behaviour is never duplicated here —
> see the [canonical skill reference](#canonical-references).

## What this is

`run_tests.py --scope changed` (and therefore `implement.py finish`'s
development-time gate, which delegates to it) does **not** always run the full
suite. For a repo whose change set can be mapped to tests, the gate runs only
the affected tests. A wall-clock measurement of that gate on a real
single-file vitest change was taken following the measurement contract in
AI_Hell's `docs/dev/test-suite-timing.md` (the contract owner) and recorded,
load-labelled, in a comment on item `SA-0MW0LNT970057DM9` and in this page —
a wall clock without its load is untrustworthy per that contract. The
AI_Hell-side doc itself is maintained in the AI_Hell repo; the framework-side
reference for the gate behaviour is this page plus `docs/dev/test-skill-reference.md`.

## The selection order

For a node/vitest repository, the changed-scope gate resolves its commands in
**exactly** this order, stopping at the first step that yields a subset:

1. **Named groups** — `.pi/test-config.json` `groups` whose `paths` match a
   changed file (smallest sufficient group; the `full` group is skipped here
   and only ever used as the fallback). SA-0MUJKF2HP001BR51.
2. **Convention mapping** — changed-file → test-file by stem:
   - a changed `*.test.ts` / `*.spec.ts` itself selects that file;
   - a changed source selects the vitest test with the same stem —
     `src/foo.ts` → `src/foo.test.ts` (or `*.spec.ts`), anywhere in the repo;
   - pytest (`test_*.py`) and node (`.test.mjs`) conventions apply the same
     way where relevant.
   - The emitted command is `node_modules/.bin/vitest run <files>` (or
     `npx --no-install vitest`) with the selected files passed explicitly, so
     a scoped run is deterministic and cache-keyed distinctly from the full
     suite. SA-0MVL7YOMJ006UMDO.
3. **Full-suite fallback** — when no subset can be selected (no group match,
   no convention-matched test file, no runnable tooling, or a
   documentation/config-only change set), the full suite runs and the reason
   is named specifically (`no changed source or test files`, `no matching
   group and no convention-matched test files`, …). A scoped run never
   silently skips testing.

TypeScript **import-graph expansion is deliberately not** part of step 2: the
TS side uses the conservative stem convention plus declared `groups`
(SA-0MVL7YOMJ006UMDO, OPEN QUESTION 1 default). A wrong graph would narrow
the run and hide breakage; the stem convention can only *under*-match, and
then the full-suite fallback still covers it — under-matching costs time, not
correctness (the full suite remains the `dev`-push gate).

## Known limits (residual gaps, filed elsewhere)

Recorded on **SA-0MVL7YOMJ006UMDO** (cross-reference comment
`SA-C0MVXW2CB8003BXPJ`), not re-opened here:

1. **Stem-only convention** — sources without a stem-mapped test fall back to
   the full suite (measured on AI_Hell: 38 of 302 non-test `src/**/*.ts`
   files, 13 %).
2. **Suffixed test names** — `PlayScene.controlScheme.test.ts` is not
   selected when the base `PlayScene.ts` changes.
3. **No repo-level `groups`** in the case-study repo, so the `--project unit`
   fast loop is not exercised by the gate.

## Proven in a real gate (AI_Hell case study)

Item comments `SA-C0MWDF2TCL009TAJN` / `SA-C0MWDF31BH005XB71` quote the
**installed** skills running
`implement.py finish`'s changed-scope step against a single-file change
(`src/core/GameState.ts`, doc-comment only) in the AI_Hell consumer repo:

- selectable change set: `{'src/core/GameState.ts'}`
- selector output: `node_modules/.bin/vitest run src/core/GameState.test.ts`
  (`selected 1 affected test file(s)`), **no fallback warning**;
- gate verdict: `scope: changed`, `exit_code: 0`, 1 test file / 23 tests
  passed, wall clock ≈ **4.5 s** green and ≈ **4.5 s** red (deliberately
  broken affected test, reverted) at host load
  `≈ 2.8 / 4.5 / 3.2 (1/5/15 min, 16 cores)` — vs the full-suite baseline of
  **346–409 s** recorded in the AI_Hell timing doc.

The full suite still runs at the `dev`-push pre-push hook, unchanged
(`run_tests.py --scope full --strict-git-env --gate-commit <sha>`), so
changed scope remains a development-time gate only.

## Canonical references

- **SA-0MVL7YOMJ006UMDO** — the framework item that owns the changed-scope
  selector (vitest `.test.ts`/`.spec.ts` mapping landed there; canonical skill
  reference). Read it for the definitive selection contract.
- `docs/dev/test-skill-reference.md` — full changed-file → test-selection
  documentation (groups, convention mapping, fallback table, resolution
  order) and `--scope changed` semantics.
- `docs/dev/implement-skill-reference.md` — how `implement.py finish` gates
  changed scope at the development-time loop.
- `docs/dev/test-suite-timing.md` (AI_Hell) — the measurement contract owner
  for every wall-clock number.
