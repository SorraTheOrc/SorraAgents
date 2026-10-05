/**
 * refresh-audits.js — ship pre-flight audit refresh (SA-0MUOO5VMH005VF69).
 *
 * Refreshes missing/stale/transient audits for `in_review` work items
 * *before* a release, so the release gates find fresh audits and do not spend
 * the release window (and the project-wide Code Freeze) serially re-auditing a
 * backlog. This is a **pre-flight-only** action: `run-release.js --refresh-audits`
 * runs it and exits without merging and without setting the Code Freeze marker.
 *
 * The refresh reuses the same conservative, bounded remediation as the gates
 * (`attemptAuditRemediation` + `RemediationBudget`), so it never runs
 * unbounded and never bypasses the audit runner's own persistence contract.
 *
 * Command boundaries are injectable for hermetic tests.
 *
 * @module refresh-audits
 */

import { execSync } from 'node:child_process';
import {
  getAuditStatus,
  attemptAuditRemediation,
  resolveAuditRunner,
} from './check-audit-gate.js';
import { getInReviewItems } from './check-final-validation.js';
import {
  RemediationBudget,
  resolveRemediationTimeoutMs,
  OFFLINE_AUDIT_REFRESH_HINT,
} from './audit-remediation.js';

/**
 * Plan which `in_review` items need an audit refresh — read-only.
 *
 * Classifies each item as:
 *   - `fresh`     — a passing, non-stale audit (nothing to do);
 *   - `toRefresh` — missing / stale / transient audit (a re-audit may fix it);
 *   - `failing`   — a fresh genuine "not ready to close" verdict (a re-audit
 *                   will not change it; the work itself needs attention).
 *
 * @param {object} [options]
 * @param {() => Array<object>} [options.getItemsFn] - `in_review` item query;
 *   defaults to {@link getInReviewItems}.
 * @param {(id: string) => string} [options.runAuditShow] - Runs
 *   `wl audit-show <id> --json`; defaults to a live query.
 * @returns {{ toRefresh: Array<{id:string,title:string,kind:string}>, fresh: Array<{id:string,title:string}>, failing: Array<{id:string,title:string,reason:string}>, errors: Array<{id:string,title:string,reason:string}> }}
 */
export function planAuditRefresh(options = {}) {
  const {
    getItemsFn = getInReviewItems,
    runAuditShow = (workItemId) => execSync(
      `wl audit-show ${workItemId} --json`,
      { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] },
    ),
  } = options;

  const items = getItemsFn();
  const toRefresh = [];
  const fresh = [];
  const failing = [];
  const errors = [];

  for (const item of items) {
    let auditData;
    try {
      auditData = JSON.parse(runAuditShow(item.id));
    } catch (err) {
      errors.push({
        id: item.id,
        title: item.title,
        reason: `Failed to query audit: ${err.stderr?.toString()?.trim() || err.message}`,
      });
      continue;
    }

    const status = getAuditStatus(item, auditData);
    const isMissing = status.isBlocking && status.reason === 'No audit found';
    if (isMissing || status.transient || status.stale) {
      const kind = isMissing ? 'missing' : (status.stale ? 'stale' : 'transient');
      toRefresh.push({ id: item.id, title: item.title, kind });
    } else if (status.isBlocking) {
      failing.push({ id: item.id, title: item.title, reason: status.reason });
    } else {
      fresh.push({ id: item.id, title: item.title });
    }
  }

  return { toRefresh, fresh, failing, errors };
}

/**
 * Refresh the audits that need it — bounded by a `RemediationBudget`.
 *
 * In `dryRun` mode the plan is returned without invoking the audit runner.
 * Otherwise each `toRefresh` item is re-audited (while the budget allows) and
 * the outcome is reported per item. Never mutates the worklog directly — the
 * audit runner persists its own results.
 *
 * @param {object} [options]
 * @param {boolean} [options.dryRun=false] - Plan only; no runner invocations.
 * @param {() => Array<object>} [options.getItemsFn] - Item query.
 * @param {(id: string) => string} [options.runAuditShow] - Audit-show boundary.
 * @param {(runnerPath: string, id: string) => string} [options.runAuditCommand] -
 *   Audit runner boundary; defaults to a live, timeout-bounded invocation.
 * @param {() => string} [options.resolveAuditRunnerFn] - Resolves the runner path.
 * @param {() => import('./audit-remediation.js').RemediationBudget} [options.createRemediationBudgetFn] -
 *   Bounded-remediation budget factory.
 * @returns {Promise<{
 *   dryRun: boolean,
 *   plan: object,
 *   refreshed: Array<{id:string,title:string,kind:string}>,
 *   stillFailing: Array<{id:string,title:string,kind:string,reason:string}>,
 *   skipped: Array<{id:string,title:string,kind:string}>,
 *   hasRemaining: boolean,
 *   message: string
 * }>}
 */
export async function refreshAudits(options = {}) {
  const {
    dryRun = false,
    getItemsFn = getInReviewItems,
    runAuditShow = (workItemId) => execSync(
      `wl audit-show ${workItemId} --json`,
      { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] },
    ),
    runAuditCommand = (runnerPath, workItemId) => execSync(
      `python3 "${runnerPath}" issue ${workItemId}`,
      { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'], timeout: resolveRemediationTimeoutMs() },
    ),
    resolveAuditRunnerFn = resolveAuditRunner,
    createRemediationBudgetFn = () => new RemediationBudget(),
  } = options;

  const plan = planAuditRefresh({ getItemsFn, runAuditShow });
  const refreshed = [];
  const stillFailing = [];
  const skipped = [];

  if (dryRun) {
    const planned = plan.toRefresh.map((i) => `${i.title} (${i.id}, ${i.kind})`);
    const lines = [
      `Pre-flight audit refresh (dry-run): ${plan.toRefresh.length} item(s) would be refreshed, `
        + `${plan.fresh.length} already fresh, ${plan.failing.length} failing.`,
    ];
    if (planned.length > 0) {
      lines.push('', 'Would refresh:');
      planned.forEach((p) => lines.push(`  - ${p}`));
    }
    if (plan.failing.length > 0) {
      lines.push('', 'Genuine failing verdicts (a re-audit will not fix these):');
      plan.failing.forEach((i) => lines.push(`  - ${i.title} (${i.id})`));
    }
    return {
      dryRun: true,
      plan,
      refreshed,
      stillFailing,
      skipped: plan.toRefresh,
      hasRemaining: plan.toRefresh.length > 0 || plan.failing.length > 0,
      message: lines.join('\n'),
    };
  }

  const budget = createRemediationBudgetFn();
  for (const item of plan.toRefresh) {
    if (!budget.canAttempt()) {
      skipped.push(item);
      continue;
    }
    console.log(`Pre-flight audit refresh: re-auditing ${item.title} (${item.id}, ${item.kind})...`);
    const remediation = await attemptAuditRemediation(item, {
      runAuditShow,
      runAuditCommand,
      resolveAuditRunnerFn,
    });
    budget.record();
    if (remediation.status === 'passing') {
      refreshed.push(item);
    } else {
      stillFailing.push({ ...item, reason: remediation.reason });
    }
  }

  const hasRemaining = stillFailing.length > 0 || skipped.length > 0 || plan.failing.length > 0;
  const lines = [
    `Pre-flight audit refresh complete: ${refreshed.length} refreshed, `
      + `${stillFailing.length} still failing, ${skipped.length} skipped (budget), `
      + `${plan.failing.length} genuine failing verdict(s), ${plan.errors.length} query error(s).`,
  ];
  if (stillFailing.length > 0 || skipped.length > 0) {
    lines.push('', `Remaining items need attention. ${OFFLINE_AUDIT_REFRESH_HINT}`);
    [...stillFailing, ...skipped].forEach((i) => lines.push(`  - ${i.title} (${i.id}, ${i.kind})`));
  }
  if (plan.failing.length > 0) {
    lines.push('', 'Genuine failing verdicts (fix the work, then re-audit):');
    plan.failing.forEach((i) => lines.push(`  - ${i.title} (${i.id})`));
  }

  return {
    dryRun: false,
    plan,
    refreshed,
    stillFailing,
    skipped,
    hasRemaining,
    message: lines.join('\n'),
  };
}

/**
 * CLI entry for the `--refresh-audits` pre-flight action.
 *
 * Prints the plan/refresh report and returns a process exit code: 0 when
 * nothing remains, 12 when items still need attention.
 *
 * @param {string[]} [cliArgs] - CLI args (reads `--dry-run`).
 * @returns {Promise<number>} Exit code.
 */
export async function runRefreshAuditsAction(cliArgs = []) {
  const dryRun = cliArgs.includes('--dry-run');
  console.log('\nPre-flight audit refresh (no release will be performed)...');
  const report = await refreshAudits({ dryRun });
  console.log(report.message);
  return report.hasRemaining ? 12 : 0;
}
