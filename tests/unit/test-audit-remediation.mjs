/**
 * Unit tests for skill/ship/scripts/audit-remediation.js
 *
 * Covers the bounded in-gate audit remediation helpers
 * (SA-0MUOO5V0P00461X8): env-configurable timeout/budget/attempt-cap
 * resolution, infrastructure-failure classification, and the
 * `RemediationBudget` wall-clock/attempt bounding.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const MODULE_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'audit-remediation.js');

test('audit-remediation: module file exists', () => {
  assert.ok(existsSync(MODULE_PATH), 'skill/ship/scripts/audit-remediation.js should exist');
});

test('audit-remediation: exports expected API', async () => {
  const mod = await import(MODULE_PATH);
  assert.equal(typeof mod.resolveRemediationTimeoutMs, 'function');
  assert.equal(typeof mod.resolveRemediationBudgetMs, 'function');
  assert.equal(typeof mod.resolveRemediationMaxItems, 'function');
  assert.equal(typeof mod.classifyRemediationError, 'function');
  assert.equal(typeof mod.RemediationBudget, 'function');
  assert.equal(typeof mod.OFFLINE_AUDIT_REFRESH_HINT, 'string');
});

// ── resolve* ─────────────────────────────────────────────────────────────────

describe('audit-remediation: env resolution', () => {
  test('defaults when unset', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(mod.resolveRemediationTimeoutMs({}), mod.DEFAULT_REMEDIATION_TIMEOUT_MS);
    assert.equal(mod.resolveRemediationBudgetMs({}), mod.DEFAULT_REMEDIATION_BUDGET_MS);
    assert.equal(mod.resolveRemediationMaxItems({}), mod.DEFAULT_REMEDIATION_MAX_ITEMS);
  });

  test('honours env overrides', async () => {
    const mod = await import(MODULE_PATH);
    const env = {
      SHIP_AUDIT_REMEDIATION_TIMEOUT_MS: '120000',
      SHIP_AUDIT_REMEDIATION_BUDGET_MS: '60000',
      SHIP_AUDIT_REMEDIATION_MAX_ITEMS: '2',
    };
    assert.equal(mod.resolveRemediationTimeoutMs(env), 120000);
    assert.equal(mod.resolveRemediationBudgetMs(env), 60000);
    assert.equal(mod.resolveRemediationMaxItems(env), 2);
  });

  test('falls back to defaults on invalid values', async () => {
    const mod = await import(MODULE_PATH);
    const env = {
      SHIP_AUDIT_REMEDIATION_TIMEOUT_MS: 'not-a-number',
      SHIP_AUDIT_REMEDIATION_BUDGET_MS: '-5',
      SHIP_AUDIT_REMEDIATION_MAX_ITEMS: '0',
    };
    assert.equal(mod.resolveRemediationTimeoutMs(env), mod.DEFAULT_REMEDIATION_TIMEOUT_MS);
    assert.equal(mod.resolveRemediationBudgetMs(env), mod.DEFAULT_REMEDIATION_BUDGET_MS);
    assert.equal(mod.resolveRemediationMaxItems(env), mod.DEFAULT_REMEDIATION_MAX_ITEMS);
  });
});

// ── classifyRemediationError ─────────────────────────────────────────────────

describe('audit-remediation: classifyRemediationError', () => {
  test('classifies ETIMEDOUT as timeout', async () => {
    const mod = await import(MODULE_PATH);
    const err = new Error('spawnSync timed out');
    err.code = 'ETIMEDOUT';
    assert.equal(mod.classifyRemediationError(err).category, 'timeout');
  });

  test('classifies SIGTERM as timeout', async () => {
    const mod = await import(MODULE_PATH);
    const err = new Error('killed');
    err.signal = 'SIGTERM';
    assert.equal(mod.classifyRemediationError(err).category, 'timeout');
  });

  test('classifies a "timed out" stderr marker as timeout', async () => {
    const mod = await import(MODULE_PATH);
    const err = new Error('exit 1');
    err.stderr = Buffer.from('Deep analysis timed out — manual review required.');
    assert.equal(mod.classifyRemediationError(err).category, 'timeout');
  });

  test('classifies a concurrency ceiling as concurrency', async () => {
    const mod = await import(MODULE_PATH);
    const err = new Error('exit 1');
    err.stderr = Buffer.from('audit_runner: host-wide audit concurrency limit reached');
    assert.equal(mod.classifyRemediationError(err).category, 'concurrency');
  });

  test('classifies a provider error as provider', async () => {
    const mod = await import(MODULE_PATH);
    const err = new Error('exit 1');
    err.stderr = Buffer.from('Pi provider error: upstream unavailable');
    assert.equal(mod.classifyRemediationError(err).category, 'provider');
  });

  test('defaults to error and surfaces stderr detail', async () => {
    const mod = await import(MODULE_PATH);
    const err = new Error('exit 2');
    err.stderr = Buffer.from('boom');
    const result = mod.classifyRemediationError(err);
    assert.equal(result.category, 'error');
    assert.equal(result.detail, 'boom');
  });
});

// ── RemediationBudget ────────────────────────────────────────────────────────

describe('audit-remediation: RemediationBudget', () => {
  test('exhausts on the attempt cap', async () => {
    const mod = await import(MODULE_PATH);
    const budget = new mod.RemediationBudget({ maxItems: 2, budgetMs: 1_000_000 });
    assert.equal(budget.canAttempt(), true);
    budget.record();
    assert.equal(budget.canAttempt(), true);
    budget.record();
    assert.equal(budget.canAttempt(), false, 'must stop after maxItems attempts');
    assert.equal(budget.exhausted, true);
    assert.match(budget.describe(), /2\/2 attempt/);
  });

  test('exhausts on the wall-clock budget (injected clock)', async () => {
    const mod = await import(MODULE_PATH);
    let now = 0;
    const budget = new mod.RemediationBudget({
      maxItems: 100,
      budgetMs: 60_000,
      now: () => now,
    });
    assert.equal(budget.canAttempt(), true);
    now = 59_000;
    assert.equal(budget.canAttempt(), true);
    now = 60_000;
    assert.equal(budget.canAttempt(), false, 'must stop once the wall clock is spent');
    assert.equal(budget.remainingMs, 0);
  });

  test('remainingMs never goes negative', async () => {
    const mod = await import(MODULE_PATH);
    let now = 0;
    const budget = new mod.RemediationBudget({ maxItems: 5, budgetMs: 1_000, now: () => now });
    now = 10_000;
    assert.equal(budget.remainingMs, 0);
  });
});

// ── C3 (WL-0MW32YGSY008GRQP): per-item timeout clamped to the remaining ────
// budget — regression for the 1865s-of-1800s remediation overshoot incident.
// Test-first pair with C4 (WL-0MW32YZP7005LZ5B); red against current HEAD.
// C4 (WL-0MW32YZP7005LZ5B) implements `RemediationBudget.clampedTimeoutMs`
// and makes the default per-item timeout differ from the total budget.

describe('audit-remediation: per-item timeout clamped to remaining budget (C3)', () => {
  test('default per-item timeout is no longer equal to the total budget', async () => {
    const mod = await import(MODULE_PATH);
    assert.notEqual(
      mod.DEFAULT_REMEDIATION_TIMEOUT_MS,
      mod.DEFAULT_REMEDIATION_BUDGET_MS,
      'one timed-out attempt must never be able to consume the whole gate budget' +
      ' (the 1865s-of-1800s incident)',
    );
  });

  test('clampedTimeoutMs clamps to the remaining budget with an injected clock', async () => {
    const mod = await import(MODULE_PATH);
    let now = 0;
    const budget = new mod.RemediationBudget({
      budgetMs: 1_800_000,
      maxItems: 5,
      now: () => now,
    });
    const perItem = mod.resolveRemediationTimeoutMs({});
    // 90 s into a 1,800 s budget: 1,710 s remain.
    now = 90_000;
    assert.equal(budget.remainingMs, 1_710_000);
    assert.equal(
      budget.clampedTimeoutMs(perItem),
      1_710_000,
      'a per-item timeout larger than the remaining budget must be clamped down',
    );
    // Fresh budget: the full per-item timeout is allowed.
    now = 0;
    assert.equal(budget.clampedTimeoutMs(perItem), perItem);
  });
});
