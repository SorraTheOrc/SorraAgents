/**
 * audit-remediation.js — bounded in-gate audit remediation for the ship skill.
 *
 * The release gates auto-remediate missing/stale/transient audits by
 * re-running `audit_runner.py issue <id>` serially (parallel spawns caused the
 * SA-0MUIUMO7T00639FL fan-out). A single audit can take 20+ minutes (Phase-1
 * parent screening alone ran 1160s in the SA-0MUOO5UEB008RXBP incident), so an
 * unbounded remediation loop over a backlog of items can hold the release (and
 * the project-wide Code Freeze) for hours.
 *
 * This module bounds that cost:
 *   - a per-item runner timeout (`SHIP_AUDIT_REMEDIATION_TIMEOUT_MS`), and
 *   - a total remediation budget across the gate run
 *     (`SHIP_AUDIT_REMEDIATION_BUDGET_MS` wall-clock and
 *     `SHIP_AUDIT_REMEDIATION_MAX_ITEMS` item cap).
 *
 * When the budget is exhausted the gate stops attempting and reports the
 * remaining items as blocking with an offline-refresh instruction, rather than
 * implying the work is "not ready to close". A remediation error is also
 * classified (`timeout` / `concurrency` / `provider` / `error`) so an
 * infrastructure failure is distinguishable from a genuine verdict.
 *
 * @module audit-remediation
 */

/** Default per-item remediation runner timeout (30 min). */
export const DEFAULT_REMEDIATION_TIMEOUT_MS = 1_800_000;
/** Default total wall-clock remediation budget per gate run (30 min). */
export const DEFAULT_REMEDIATION_BUDGET_MS = 1_800_000;
/** Default maximum number of in-gate remediation attempts per gate run. */
export const DEFAULT_REMEDIATION_MAX_ITEMS = 5;

export const REMEDIATION_TIMEOUT_ENV = 'SHIP_AUDIT_REMEDIATION_TIMEOUT_MS';
export const REMEDIATION_BUDGET_ENV = 'SHIP_AUDIT_REMEDIATION_BUDGET_MS';
export const REMEDIATION_MAX_ITEMS_ENV = 'SHIP_AUDIT_REMEDIATION_MAX_ITEMS';

/**
 * Guidance shown when in-gate remediation is skipped or exhausted. The correct
 * place to refresh a backlog of audits is outside the release window.
 */
export const OFFLINE_AUDIT_REFRESH_HINT =
  'Refresh audits outside the release: '
  + 'python3 skill/audit/scripts/audit_runner.py batch '
  + '(or run the audits individually), then re-run the release.';

/**
 * Resolve a positive-integer override from an env-like object, falling back to
 * a default when unset or invalid (a misconfigured value must never break a
 * release).
 *
 * @param {string|undefined} raw - Raw env value.
 * @param {number} defaultValue - Fallback value.
 * @returns {number}
 */
function resolvePositiveInt(raw, defaultValue) {
  if (raw === undefined || raw === null || raw === '') {
    return defaultValue;
  }
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return defaultValue;
  }
  return parsed;
}

/**
 * Resolve the per-item remediation runner timeout (ms).
 *
 * @param {Record<string, string|undefined>} [env] - Environment (defaults to
 *   `process.env`; injectable for tests).
 * @returns {number}
 */
export function resolveRemediationTimeoutMs(env = process.env) {
  return resolvePositiveInt(env[REMEDIATION_TIMEOUT_ENV], DEFAULT_REMEDIATION_TIMEOUT_MS);
}

/**
 * Resolve the total wall-clock remediation budget (ms) for a gate run.
 *
 * @param {Record<string, string|undefined>} [env] - Environment.
 * @returns {number}
 */
export function resolveRemediationBudgetMs(env = process.env) {
  return resolvePositiveInt(env[REMEDIATION_BUDGET_ENV], DEFAULT_REMEDIATION_BUDGET_MS);
}

/**
 * Resolve the maximum number of in-gate remediation attempts per gate run.
 *
 * @param {Record<string, string|undefined>} [env] - Environment.
 * @returns {number}
 */
export function resolveRemediationMaxItems(env = process.env) {
  return resolvePositiveInt(env[REMEDIATION_MAX_ITEMS_ENV], DEFAULT_REMEDIATION_MAX_ITEMS);
}

/**
 * Classify a remediation-runner failure so an infrastructure problem is
 * distinguishable from a genuine audit verdict.
 *
 * Categories:
 *   - `timeout`     — the runner was killed after the per-item timeout
 *                     (`err.code === 'ETIMEDOUT'`, `SIGTERM`/`SIGKILL`, or a
 *                     "timed out" marker).
 *   - `concurrency` — the audit concurrency ceiling was saturated.
 *   - `provider`    — the model provider returned an error / stop reason.
 *   - `error`       — anything else.
 *
 * @param {Error & { stderr?: Buffer|string, signal?: string, code?: string }} err
 * @returns {{ category: 'timeout'|'concurrency'|'provider'|'error', detail: string }}
 */
export function classifyRemediationError(err) {
  const detail = (err && err.stderr ? err.stderr.toString() : '')?.trim()
    || (err && err.message) || String(err);
  const code = err && err.code ? String(err.code) : '';
  const signal = err && err.signal ? String(err.signal) : '';

  if (
    code === 'ETIMEDOUT'
    || signal === 'SIGTERM'
    || signal === 'SIGKILL'
    || /\btime(?:d)?\s*outs?\b/i.test(detail)
  ) {
    return { category: 'timeout', detail };
  }
  if (/concurrency limit reached/i.test(detail)) {
    return { category: 'concurrency', detail };
  }
  if (/provider\s+(?:error|stop\s+reason)/i.test(detail)) {
    return { category: 'provider', detail };
  }
  return { category: 'error', detail };
}

/**
 * A bounded remediation budget for one gate run.
 *
 * The budget is exhausted when either the wall-clock budget is spent or the
 * attempt cap is reached. `now` is injectable so tests are deterministic.
 */
export class RemediationBudget {
  /**
   * @param {object} [options]
   * @param {number} [options.budgetMs] - Wall-clock budget (defaults to env).
   * @param {number} [options.maxItems] - Attempt cap (defaults to env).
   * @param {() => number} [options.now] - Clock (defaults to `Date.now`).
   */
  constructor({ budgetMs, maxItems, now } = {}) {
    this.budgetMs = budgetMs ?? resolveRemediationBudgetMs();
    this.maxItems = maxItems ?? resolveRemediationMaxItems();
    this._now = now ?? (() => Date.now());
    this._start = this._now();
    this.attempts = 0;
  }

  /** Wall-clock ms elapsed since the budget was created. */
  get elapsedMs() {
    return Math.max(0, this._now() - this._start);
  }

  /** Wall-clock ms remaining in the budget (never negative). */
  get remainingMs() {
    return Math.max(0, this.budgetMs - this.elapsedMs);
  }

  /** True when no further attempt may be made. */
  get exhausted() {
    return this.attempts >= this.maxItems || this.remainingMs <= 0;
  }

  /** @returns {boolean} Whether another remediation attempt is allowed. */
  canAttempt() {
    return !this.exhausted;
  }

  /** Record a completed (or attempted) remediation run. */
  record() {
    this.attempts += 1;
  }

  /** Human-readable budget status (attempts and elapsed/limit). */
  describe() {
    const elapsedS = Math.round(this.elapsedMs / 1000);
    const budgetS = Math.round(this.budgetMs / 1000);
    return `${this.attempts}/${this.maxItems} attempt(s), ${elapsedS}s of ${budgetS}s`;
  }
}
