/**
 * Tests for skill/ship/scripts/timing.js — JS timing helper for skill scripts
 * (SA-0MTACSN1K002TN82, parent SA-0MT319YGQ002E801).
 *
 * Run with:
 *   node tests/node/test-timing.mjs
 */

import { strict as assert } from 'node:assert';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { Timer, nanosToSeconds, secondsToNanos } from '../../skill/ship/scripts/timing.js';

// ── Helpers ─────────────────────────────────────────────────────────────────

function test(name, fn) {
  try {
    fn();
    console.log(`  ✓ ${name}`);
  } catch (err) {
    console.error(`  ✗ ${name}\n    ${err.message}`);
    process.exitCode = 1;
  }
}

// ── Tests ────────────────────────────────────────────────────────────────────

test('nanosToSeconds converts nanoseconds to seconds', () => {
  assert.equal(nanosToSeconds(1_000_000_000n), 1.0);
  assert.equal(nanosToSeconds(500_000_000n), 0.5);
});

test('secondsToNanos converts seconds to nanoseconds', () => {
  assert.equal(secondsToNanos(1.0), 1_000_000_000n);
  assert.equal(secondsToNanos(0.5), 500_000_000n);
});

test('Timer records non-zero elapsed time', () => {
  const t = new Timer('step');
  t.start();
  // Force some elapsed time
  const start = process.hrtime.bigint();
  while (process.hrtime.bigint() - start < 1_000_000n) {} // ~1ms busy wait
  const seconds = t.stop();
  assert.ok(seconds > 0, `expected elapsed > 0, got ${seconds}`);
});

test('Timer totalSeconds equals elapsed for leaf', () => {
  const t = new Timer('leaf');
  t.start();
  t.stop();
  assert.ok(t.totalSeconds > 0);
});

test('Nested timers roll up into parent', () => {
  const parent = new Timer('parent');
  parent.start();
  const a = new Timer('child_a', parent);
  a.start();
  const start = process.hrtime.bigint();
  while (process.hrtime.bigint() - start < 1_000_000n) {}
  a.stop();
  const b = new Timer('child_b', parent);
  b.start();
  b.stop();
  parent.stop();
  assert.equal(parent.nestedSteps.length, 2);
  const sum = parent.nestedSteps.reduce((acc, s) => acc + s.totalSeconds, 0);
  assert.ok(Math.abs(parent.totalSeconds - sum) < 0.01, 'parent total ≈ sum of children');
});

test('Timer percentage is 100 for single root', () => {
  const t = new Timer('only');
  t.start();
  t.stop();
  assert.ok(Math.abs(t.percentage - 100) < 0.1, `expected ~100%, got ${t.percentage}`);
});

test('render produces human-readable output', () => {
  const t = new Timer('my_step');
  t.start();
  t.stop();
  const report = t.render();
  assert.ok(typeof report === 'string');
  assert.ok(report.includes('Timing Report'));
  assert.ok(report.includes('my_step'));
  assert.ok(report.includes('Total'));
});

test('render shows nested steps', () => {
  const parent = new Timer('parent');
  parent.start();
  const child = new Timer('child', parent);
  child.start();
  child.stop();
  parent.stop();
  const report = parent.render();
  assert.ok(report.includes('child'));
});

test('toDict produces JSON-serializable structure', () => {
  const root = new Timer('root');
  root.start();
  const a = new Timer('a', root);
  a.start();
  a.stop();
  root.stop();
  const d = root.toDict();
  assert.equal(typeof d, 'object');
  assert.equal(d.name, 'root');
  assert.ok(Array.isArray(d.nested_steps));
  assert.equal(d.nested_steps.length, 1);
  assert.equal(d.nested_steps[0].name, 'a');
  // Must not throw
  JSON.stringify(d);
});

test('toJson produces valid JSON string', () => {
  const t = new Timer('step');
  t.start();
  t.stop();
  const json = t.toJson();
  assert.equal(typeof json, 'string');
  const parsed = JSON.parse(json);
  assert.equal(parsed.name, 'step');
});

test('sibling percentages sum to ~100%', () => {
  const parent = new Timer('parent');
  parent.start();
  const a = new Timer('a', parent);
  a.start();
  const s1 = process.hrtime.bigint();
  while (process.hrtime.bigint() - s1 < 1_000_000n) {}
  a.stop();
  const b = new Timer('b', parent);
  b.start();
  b.stop();
  parent.stop();
  const sum = a.percentage + b.percentage;
  assert.ok(Math.abs(sum - 100) < 1.0, `expected ~100%, got ${sum}`);
});