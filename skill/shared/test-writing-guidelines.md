# Test Writing Guidelines — Anti-Patterns and Positive Guidance

> Source: Audit of the Tableau-Card-Engine test suite (344 files) that removed
> 32 low-value test files (~3,500 lines).

## Anti-Patterns to Avoid

### 1. Source-Code-Grep Tests

**What it looks like:** `readFileSync` + `toContain` / string-index assertions
on source files. The test asserts file text, not behaviour.

**Why it's bad:** Breaks on any refactor without detecting regressions; can
match itself.

**Example:** `GymHandPileSpacing.test.ts`, `FeudalismTurnController.reducedMotion`,
`GolfSoundDuplication.test.ts` (~16 files removed).

**Fix:** Assert the *observable behaviour* that the source code is supposed to
produce (e.g. the emitted event, the rendered output, the state transition).

### 2. Placeholder Tests

**What it looks like:** `expect(true).toBe(true)` with `// TODO` comments, or
tests with zero assertions.

**Example:** `GolfAnimator.reducedMotion.test.ts` had 8/8 placeholder tests;
`ui/ReducedMotion.test.ts` had 6 placeholders + 2 empty tests.

**Why it's bad:** They always pass — they give a false sense of coverage.

**Fix:** If the feature is not yet implemented, track the work in a work item
instead of adding a placeholder test.

### 3. Self-Referential Simulations

**What it looks like:** The test re-implements the scene / game logic inside
the test file and asserts against the test's own copy of the logic.

**Example:** `GymHandPileClickToPlay.test.ts` (461 lines) and
`GymSaveLoadScreenshotFilter.test.ts` (imports zero production code).

**Why it's bad:** The test passes even when the real production code diverges
from the simulated copy — it asserts nothing about the actual code.

**Fix:** Import and exercise the real production code. If the production code
is not yet available, create a work item rather than writing a simulation.

### 4. Duplicates of Existing Core Coverage

**What it looks like:** Re-testing the same class or function from a different
import path, wrapping the same assertions under a new test name.

**Example:** `GymAudioFeedback` x2 duplicated core `SoundManager.test.ts`;
`visibility-ownership-runtime` duplicated core `VisibilityOwnership.test.ts`;
`GymReducedMotion` duplicated `SettingsStore.test.ts`.

**Why it's bad:** No new coverage is gained; maintenance burden doubles.

**Fix:** If a behaviour is already tested via a public API, do not add another
test file for it. Enhance the existing test instead.

### 5. Type-Level / Structural-Only Tests

**What it looks like:** Asserting that an object literal `satisfies` an
interface the TypeScript compiler already enforces.

**Example:** `DebugToolsRegistry` x2, `GameEventLogOverlay`, `SessionExportTool`,
`StateInspectorOverlay`.

**Why it's bad:** The compiler checks this at build time; the test adds no
runtime value.

**Fix:** Remove the test. If runtime shape validation is needed, use a proper
runtime schema library (e.g. Zod) and assert against that.

### 6. Zero-Assertion Browser / Scene Tests

**What it looks like:** Booting a scene or browser context but never asserting
anything about the result.

**Example:** `GymTooltipScene.browser.test.ts`.

**Why it's bad:** It exercises the boot path but proves nothing.

**Fix:** Add at least one real assertion (prefer multiple) — e.g. verify a
specific DOM node exists, a state value is correct, an event was emitted.

## Positive Guidance

1. **Every test must assert observable behaviour of production code:** input →
   output, state transitions, emitted events.
2. **If a behaviour can be tested via the public API, never inspect source
   text.**
3. **Tests that duplicate an existing core test should be removed, not re-written
   under a new name.**
4. **When a feature is not yet implemented, do NOT add placeholder tests; track
   the work in a work item instead.**
5. **Browser / scene tests must contain at least one real assertion** (prefer
   multiple).
6. **A test file that imports no production code is a smell — delete it.**
