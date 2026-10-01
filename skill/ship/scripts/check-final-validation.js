/**
 * check-final-validation.js — Final validation sweep for the ship skill.
 *
 * The release process runs several scoped gates before promoting `dev` to
 * `main`:
 *
 *   1. Unmerged branches (exit 3)
 *   2. Audit readiness (exit 6) — **top-level** `in_review` items only
 *   3. Critical items (exit 7)
 *   4. Worklog refs (exit 8)
 *   5. Producer review (exit 9) — **top-level** `in_review` items only
 *
 * None of those gates perform a comprehensive sweep of *all* `in_review`
 * items (both top-level and child items) for audit health. This module adds
 * that final sweep (SA-0MTMSPKEX003JGIX, Step 3.7): it queries **every**
 * `in_review` item — with no `parentId` filter — and blocks the release
 * (exit code 12) when any item is found with:
 *
 *   a. a missing audit (no audit record exists),
 *   b. a stale audit (audit exists but predates the item's last update),
 *   c. a failing audit (a fresh "not ready to close" verdict), or
 *   d. a producer-review flag (`needsProducerReview === true`).
 *
 * Missing, stale, and transient audits are auto-remediated conservatively by
 * re-running the audit runner (`audit_runner.py issue <id>`, which persists
 * per its own contract — this gate never calls `wl update` directly) and
 * re-checking `wl audit-show`. Genuine "not ready to close" verdicts block
 * immediately with no re-audit attempt. Producer-review flags are reported
 * with the command needed to clear them.
 *
 * **Escalation for uncovered children (SA-0MUJLWPB10038Z8Q):** When an
 * uncovered child has `needsProducerReview === true` and no audit-ready
 * `in_review` ancestor, the gate escalates by setting `needsProducerReview`
 * `true` on the nearest `in_review` ancestor (or top-level parent) and adds
 * a comment enumerating the child id(s). Because the producer-review gate
 * (Step 3.6, exit 9) has already run, the escalated ancestor is surfaced as a
 * **blocking item** so the current release fails this gate (exit 12); the flag
 * also makes the ancestor fail the producer-review gate (exit 9) on the next
 * run. The child is removed from the blocking list once escalated (it no
 * longer blocks independently — its parent does). Escalation requires an
 * injectable `runCloseCommand` boundary (used for `wl update`); tests must
 * inject a no-op to remain hermetic.
 *
 * Command boundaries (`getItemsFn`, `runAuditShow`, `runAuditCommand`,
 * `resolveAuditRunnerFn`, `runCloseCommand`, `getItemByIdFn`) are injectable
 * so unit tests are hermetic — no live `wl`/`audit_runner` invocations.
 */

import { execSync } from 'node:child_process';
import {
  getAuditStatus,
  buildRemediationCommand,
  buildProducerReviewRemediationCommand,
  resolveAuditRunner,
  attemptAuditRemediation,
  queryContentFreshness,
  hasContentFingerprint,
} from './check-audit-gate.js';
import {
  RemediationBudget,
  resolveRemediationTimeoutMs,
  OFFLINE_AUDIT_REFRESH_HINT,
} from './audit-remediation.js';

// Shared audit-freshness heuristics (parseIsoUtc, isAuditStale) and the
// freshness constants now live in ./audit-freshness.js so both this gate
// (Step 3.7) and check-audit-gate.js (Step 2) apply identical staleness
// semantics without an import cycle (check-final-validation already imports
// from check-audit-gate). Re-exported here for backward compatibility —
// existing consumers and unit tests import these from this module
// (SA-0MUOO5UEB008RXBP).
export {
  AUDIT_FRESHNESS_BUFFER_SECONDS,
  AUDIT_PERSIST_WRITE_TOLERANCE_SECONDS,
  parseIsoUtc,
  isAuditStale,
} from './audit-freshness.js';

// ── shellQuote ───────────────────────────────────────────────────────────────

/**
 * Quote a single argument for safe interpolation into a shell command.
 *
 * Wraps the value in single quotes and escapes any embedded single quote.
 * Used by the default escalation boundary so a comment containing spaces or
 * punctuation is passed as one argument to `wl` (not re-split by the shell).
 *
 * @param {string} value - The raw argument value.
 * @returns {string} The shell-safe quoted argument.
 */
export function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

// ── classifyAudit ────────────────────────────────────────────────────────────

/**
 * Classify a work item's audit into one of five kinds.
 *
 * Precedence (first match wins):
 *   1. `missing`   — no audit record exists.
 *   2. `transient` — audit timed out / provider error / FailureNotice.
 *   3. `stale`     — audit exists but is outdated (see
 *      {@link module:audit-freshness.isAuditStale}).
 *   4. `failing`   — a fresh audit with a genuine "not ready to close" verdict.
 *   5. `passing`   — a fresh audit that is ready to close.
 *
 * Staleness takes precedence over the verdict: a stale verdict is not
 * trustworthy, so a stale "not ready to close" is remediated (and re-blocked
 * only if it still fails) rather than blocked immediately. The classification
 * is delegated to `getAuditStatus()` (shared with Step 2) so both release
 * gates apply identical precedence (SA-0MUOO5UEB008RXBP).
 *
 * @param {{ id: string, title: string, updatedAt?: string|null }} workItem -
 *   The work item being checked.
 * @param {object|null} auditData - Parsed `wl audit-show <id> --json` payload.
 * @returns {{ kind: 'missing'|'transient'|'stale'|'failing'|'passing', reason: string, summary: string|null }}
 */
export function classifyAudit(workItem, auditData) {
  const status = getAuditStatus(workItem, auditData);

  if (status.isBlocking && status.reason === 'No audit found') {
    return { kind: 'missing', reason: status.reason, summary: status.summary || null };
  }
  if (status.transient) {
    return { kind: 'transient', reason: status.reason, summary: status.summary || null };
  }
  if (status.stale) {
    return {
      kind: 'stale',
      reason: 'Audit is stale (performed before the work item was last updated)',
      summary: status.summary || null,
    };
  }
  if (status.isBlocking) {
    return { kind: 'failing', reason: status.reason, summary: status.summary || null };
  }
  return { kind: 'passing', reason: status.reason, summary: status.summary || null };
}

// ── getInReviewItems ─────────────────────────────────────────────────────────

/**
 * Query Worklog for **all** `in_review` work items (top-level and children).
 *
 * Unlike `getTopLevelCandidateItems()` (which scopes the existing release
 * gates to `parentId == null`), this deliberately applies no `parentId`
 * filter: the final validation sweep must catch audit gaps on child items
 * too (SA-0MTMSPKEX003JGIX AC1).
 *
 * The query is piped through `jq` so only the projected fields cross into
 * Node's buffer (mirroring `getCandidateItems()`), and runs under
 * `bash -c` for `set -o pipefail` support. `updatedAt` is projected so
 * {@link isAuditStale} can evaluate freshness without an extra `wl show`.
 *
 * @returns {Array<{ id: string, title: string, needsProducerReview: boolean|null, parentId: string|null, updatedAt: string|null }>}
 */
export function getInReviewItems() {
  try {
    const output = execSync(
      `bash -c 'set -o pipefail; wl list --stage in_review --json ` +
      `| jq -c "[.workItems[] | {id, title, needsProducerReview, parentId, updatedAt}]"'`,
      { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] },
    );
    const projected = JSON.parse(output);
    if (!Array.isArray(projected)) {
      return [];
    }
    return projected.map((item) => ({
      id: item.id,
      title: item.title || item.id,
      needsProducerReview: item.needsProducerReview !== undefined
        ? item.needsProducerReview
        : null,
      parentId: item.parentId !== undefined ? item.parentId : null,
      updatedAt: item.updatedAt !== undefined ? item.updatedAt : null,
    }));
  } catch (err) {
    console.error(
      `Warning: Failed to query in_review items for final validation: ${err.message}`,
    );
    return [];
  }
}

// ── getItemById ──────────────────────────────────────────────────────────────

/**
 * Resolve a single work item (any stage) by id — used to walk a child's
 * parent chain for parent-coverage / out-of-scope resolution.
 *
 * Returns `null` when the item genuinely does not exist (deleted/missing);
 * throws on a command failure so the caller can treat the child as
 * *uncovered* (evaluate its own audit) rather than silently excluding it.
 *
 * @param {string} itemId - Work item id.
 * @returns {{id: string, title: string, stage: string|null, parentId: string|null, updatedAt: string|null}|null}
 */
export function getItemById(itemId) {
  try {
    const output = execSync(`wl show ${itemId} --json`, {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    const parsed = JSON.parse(output);
    if (!parsed || parsed.success === false || !parsed.workItem) {
      return null;
    }
    const wi = parsed.workItem;
    return {
      id: wi.id || itemId,
      title: wi.title || wi.id || itemId,
      stage: wi.stage !== undefined ? wi.stage : null,
      parentId: wi.parentId !== undefined ? wi.parentId : null,
      updatedAt: wi.updatedAt !== undefined ? wi.updatedAt : null,
    };
  } catch (err) {
    const stdout = err && err.stdout ? err.stdout.toString() : '';
    if (/"success"\s*:\s*false/.test(stdout) || /not found/i.test(stdout)) {
      return null;
    }
    throw err;
  }
}

// ── findInReviewAncestor ─────────────────────────────────────────────────────

/**
 * Find the nearest `in_review` ancestor of a work item.
 *
 * Walks the parent chain from the given item's `parentId` upward, returning
 * the first ancestor whose stage is `in_review`. Returns `null` when no
 * such ancestor exists.
 *
 * @param {{id: string, parentId: string|null}} item - The child work item.
 * @param {(id: string) => (object|null)} getItemByIdFn - Work item resolver.
 * @returns {string|null} The nearest `in_review` ancestor id, or `null`.
 */
export function findInReviewAncestor(item, getItemByIdFn) {
  const visited = new Set([item.id]);
  let ancestorId = item.parentId;
  while (ancestorId) {
    if (visited.has(ancestorId)) {
      return null; // cycle detected — don't escalate
    }
    visited.add(ancestorId);
    let ancestor;
    try {
      ancestor = getItemByIdFn(ancestorId);
    } catch {
      return null; // resolution failure — fall back to blocking directly
    }
    if (!ancestor) {
      return null; // ancestor doesn't exist — no escalation target
    }
    if (ancestor.stage === 'in_review') {
      return ancestorId;
    }
    ancestorId = ancestor.parentId;
  }
  return null;
}

// ── resolveChildScope ────────────────────────────────────────────────────────

/**
 * Resolve a child item's release scope by walking its parent chain
 * (SA-0MU2OY1N9000XL2H).
 *
 * Precedence (first match wins):
 *   1. A parent that does not exist (deleted) → `excluded`.
 *   2. An ancestor whose stage is not `in_review` → `excluded`
 *      (the subtree is not part of the release).
 *   3. The nearest `in_review` ancestor with a passing audit
 *      (`readyToClose === true`) → `covered` (the child is covered by the
 *      parent's audit/review). The Step-2 audit gate is authoritative for
 *      top-level readiness, so a passing parent audit covers even when the
 *      conservative time-gate heuristic would label it stale.
 *   4. Otherwise → `uncovered` (evaluate the child's own audit/flag).
 *
 * A missing, transient, or failing (`readyToClose !== true`) `in_review`
 * ancestor does NOT provide coverage; the walk continues to the next ancestor so
 * a higher passing `in_review` ancestor can still cover the child. Cycles are
 * broken conservatively (`uncovered`).
 *
 * @param {{id: string, parentId: string|null}} item - The child item.
 * @param {object} boundaries - Injectable command boundaries.
 * @param {(id: string) => (object|null)} boundaries.getItemByIdFn
 * @param {(id: string) => string} boundaries.runAuditShow
 * @returns {{outcome: 'covered'|'excluded'|'uncovered', reason: string, ancestorId: string|null, parentStage: string|null}}
 */
export function resolveChildScope(item, { getItemByIdFn, runAuditShow }) {
  const visited = new Set([item.id]);
  let ancestorId = item.parentId;
  while (ancestorId) {
    if (visited.has(ancestorId)) {
      return {
        outcome: 'uncovered',
        reason: `parent cycle detected at ${ancestorId}`,
        ancestorId,
        parentStage: null,
      };
    }
    visited.add(ancestorId);

    let parentItem;
    try {
      parentItem = getItemByIdFn(ancestorId);
    } catch (err) {
      return {
        outcome: 'uncovered',
        reason: `failed to resolve parent ${ancestorId}: ${err.message}`,
        ancestorId,
        parentStage: null,
      };
    }
    if (parentItem == null) {
      return {
        outcome: 'excluded',
        reason: `parent ${ancestorId} does not exist (deleted/missing)`,
        ancestorId,
        parentStage: null,
      };
    }
    if (parentItem.stage !== 'in_review') {
      return {
        outcome: 'excluded',
        reason: `parent ${ancestorId} is not in_review (stage=${parentItem.stage ?? 'unknown'})`,
        ancestorId,
        parentStage: parentItem.stage ?? null,
      };
    }

    let auditData = null;
    try {
      auditData = JSON.parse(runAuditShow(ancestorId));
    } catch (_err) {
      return {
        outcome: 'uncovered',
        reason: `failed to query parent ${ancestorId} audit`,
        ancestorId,
        parentStage: parentItem.stage,
      };
    }
    const parentStatus = getAuditStatus(parentItem, auditData);
    // Coverage requires a *passing* parent audit (readyToClose === true). A
    // stale/non-passing in_review ancestor does not cover the child — the walk
    // continues up the chain. Using the explicit `passing` flag (rather than
    // the old `!isBlocking && !transient` pair) keeps this correct now that
    // getAuditStatus() classifies a stale failing audit as non-blocking
    // (SA-0MUOO5UEB008RXBP).
    if (parentStatus.passing) {
      return {
        outcome: 'covered',
        reason: `covered by passing parent audit ${ancestorId}`,
        ancestorId,
        parentStage: parentItem.stage,
      };
    }
    // Non-passing in_review ancestor: look further up the chain.
    ancestorId = parentItem.parentId;
  }
  return {
    outcome: 'uncovered',
    reason: 'no in_review ancestor with a passing audit',
    ancestorId: null,
    parentStage: null,
  };
}

// ── checkFinalValidation ─────────────────────────────────────────────────────

/**
 * Run the final validation sweep over **all** `in_review` work items.
 *
 * For each item:
 *   - `needsProducerReview === true` → blocking (AC4) with remediation. An
 *     **uncovered child** with the flag set is escalated to its nearest
 *     `in_review` ancestor instead (SA-0MUJLWPB10038Z8Q): the ancestor is
 *     flagged `needsProducerReview=true` with a comment enumerating the
 *     child(ren), and the child is removed from the blocking list (the
 *     parent now blocks the producer-review gate).
 *   - audit `missing` / `stale` / `transient` → auto-remediate via the audit
 *     runner, then re-check; pass → unblocked, still failing → blocking
 *     (AC2).
 *   - audit `failing` (fresh, not transient) → blocking immediately, no
 *     re-audit attempt (AC3).
 *
 * Returns a report whose `message` lists every blocking item with its reason
 * and an actionable remediation command (AC5). The caller
 * (`run-release.js` Step 3.7) maps `hasBlockingItems` to exit code 12.
 *
 * @param {object} [options] - Optional injection point (used by unit tests).
 * @param {() => Array<object>} [options.getItemsFn] - All-`in_review`-item
 *   query; defaults to {@link getInReviewItems}.
 * @param {(workItemId: string) => string} [options.runAuditShow] - Runs
 *   `wl audit-show <id> --json` and returns the raw JSON string.
 * @param {(runnerPath: string, workItemId: string) => string} [options.runAuditCommand] -
 *   Runs the audit remediation (`python3 <runner> issue <id>`).
 * @param {() => string} [options.resolveAuditRunnerFn] - Resolves the audit
 *   runner script path; defaults to `resolveAuditRunner`.
 * @param {(id: string) => (object|null)} [options.getItemByIdFn] - Resolves a
 *   work item by id (for parent-chain coverage); defaults to {@link getItemById}.
 * @param {(itemId: string, args: string[]) => string} [options.runCloseCommand] -
 *   Runs a `wl` sub-command (used for the escalation `wl update`); defaults
 *   to `wl <args> --json`. Tests inject a no-op to stay hermetic.
 * @param {boolean} [options.dryRun=false] - When true, planned escalations
 *   are reported but no `wl` mutation is performed and escalation does not
 *   block (AC4).
 * @returns {Promise<{
 *   hasBlockingItems: boolean,
 *   blockingItems: Array<{
 *     workItemId: string,
 *     title: string,
 *     reasons: string[],
 *     reason: string,
 *     summary: string|null,
 *     remediation: string
 *   }>,
 *   remediatedItems: Array<{ workItemId: string, title: string, reason: string }>,
 *   escalatedItems: Array<{ workItemId: string, title: string, ancestorId: string, reason: string, planned: boolean }>,
 *   plannedOverrides: Array<{ workItemId: string, title: string, ancestorId: string, reason: string }>,
 *   coveredChildren: Array<{ workItemId: string, title: string, parentId: string|null, reason: string }>,
 *   excludedChildren: Array<{ workItemId: string, title: string, parentId: string|null, parentStage: string|null, reason: string }>,
 *   passingCount: number,
 *   dryRun: boolean,
 *   message: string
 * }>}
 */
export async function checkFinalValidation(options = {}) {
  const {
    getItemsFn = getInReviewItems,
    runAuditShow = (workItemId) => execSync(
      `wl audit-show ${workItemId} --json`,
      { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] },
    ),
    runAuditCommand = (runnerPath, workItemId) => execSync(
      `python3 "${runnerPath}" issue ${workItemId}`,
      // Per-item timeout guard, consistent with the audit gate's cap.
      // Configurable via SHIP_AUDIT_REMEDIATION_TIMEOUT_MS
      // (SA-0MUOO5V0P00461X8).
      { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'], timeout: resolveRemediationTimeoutMs() },
    ),
    resolveAuditRunnerFn = resolveAuditRunner,
    createRemediationBudgetFn = () => new RemediationBudget(),
    skipRemediation = false,
    queryContentFreshnessFn = queryContentFreshness,
    getItemByIdFn = getItemById,
    runCloseCommand = (itemId, args) => execSync(
      `wl ${args.map(shellQuote).join(' ')} --json`,
      { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] },
    ),
    dryRun = false,
  } = options;

  const items = getItemsFn();

  if (items.length === 0) {
    return {
      hasBlockingItems: false,
      blockingItems: [],
      remediatedItems: [],
      escalatedItems: [],
      plannedOverrides: [],
      coveredChildren: [],
      excludedChildren: [],
      passingCount: 0,
      dryRun,
      message: 'No in_review work items found. Final-validation gate passed.',
    };
  }

  const blockingItems = [];
  const remediatedItems = [];
  const escalatedItems = [];
  const plannedOverrides = [];
  const coveredChildren = [];
  const excludedChildren = [];
  let passingCount = 0;
  const budget = createRemediationBudgetFn();

  for (const item of items) {
    // ── Child scope: parent coverage / out-of-scope exclusion ───────────
    // Top-level items (parentId == null) are always evaluated. A child is
    // skipped when covered by a passing in_review parent audit or excluded
    // (parent deleted / not in_review); it never blocks in those cases.
    if (item.parentId) {
      const scope = resolveChildScope(item, { getItemByIdFn, runAuditShow });
      if (scope.outcome === 'covered') {
        coveredChildren.push({
          workItemId: item.id,
          title: item.title,
          parentId: scope.ancestorId,
          reason: scope.reason,
        });
        // Covered child flagged for producer review → the close step performs
        // the override (SA-0MUJLWPB10038Z8Q AC1). Report it as planned here so
        // `--dry-run` surfaces the intended overrides (AC4). The real override
        // (flag clear + comment) happens in `closeWorkItemsAfterRelease`.
        if (item.needsProducerReview === true) {
          plannedOverrides.push({
            workItemId: item.id,
            title: item.title,
            ancestorId: scope.ancestorId,
            reason: 'covered by audit-ready in_review parent audit',
          });
        }
        continue;
      }
      if (scope.outcome === 'excluded') {
        excludedChildren.push({
          workItemId: item.id,
          title: item.title,
          parentId: scope.ancestorId,
          parentStage: scope.parentStage,
          reason: scope.reason,
        });
        continue;
      }
      // 'uncovered' → fall through and evaluate the child's own audit/flag.
    }

    const reasons = [];
    let summary = null;
    let remediated = false;
    let producerReview = false;
    let auditIssue = false;

    // ── Producer-review flag (AC1d, AC2, AC4) ─────────────────────────
    if (item.needsProducerReview === true) {
      // Escalation for uncovered children (SA-0MUJLWPB10038Z8Q AC2): set
      // needsProducerReview=true on the nearest in_review ancestor with a
      // comment enumerating the child id(s). The parent then blocks the
      // producer-review gate. Only applies to uncovered children (parentId
      // is set and resolveChildScope returned 'uncovered').
      if (item.parentId) {
        const ancestorId = findInReviewAncestor(item, getItemByIdFn);
        if (ancestorId) {
          const escalationMsg =
            `Uncovered child ${item.id} (${item.title}) requires producer review; `
            + `escalating to in_review ancestor ${ancestorId}.`;
          escalatedItems.push({
            workItemId: item.id,
            title: item.title,
            ancestorId,
            reason: 'needsProducerReview escalated to in_review ancestor',
            planned: dryRun,
          });
          if (dryRun) {
            // `--dry-run` must not mutate the worklog (AC4).
            console.log(
              `Final-validation gate (dry-run): would escalate ${item.id} `
              + `needsProducerReview to ancestor ${ancestorId}.`,
            );
            continue;
          }
          try {
            runCloseCommand(ancestorId, [
              'update', ancestorId,
              '--needs-producer-review', 'true',
              '--comment', `final-validation: ${escalationMsg}`,
            ]);
            console.log(
              `Final-validation gate: escalated ${item.id} needsProducerReview to ancestor ${ancestorId}.`,
            );
            // Child no longer blocks independently — parent does.
            continue;
          } catch (err) {
            console.warn(
              `Final-validation gate: escalation of ${item.id} to ${ancestorId} failed: ${err.message}. Blocking directly.`,
            );
            escalatedItems.pop();
          }
        }
      }
      producerReview = true;
      reasons.push('Flagged for producer review');
    }

    // ── Audit evaluation (AC1a–c, AC2, AC3) ────────────────────────────
    let auditData = null;
    let queryFailed = false;
    try {
      auditData = JSON.parse(runAuditShow(item.id));
    } catch (err) {
      queryFailed = true;
      auditIssue = true;
      reasons.push(
        `Failed to query audit: ${err.stderr?.toString()?.trim() || err.message}`,
      );
    }

    if (!queryFailed) {
      let classification = classifyAudit(item, auditData);

      // Content-fingerprint fast path (SA-0MUOO5W8J001DYTI): a time-stale audit
      // whose stored content fingerprint still matches needs no re-audit. Ask
      // the runner read-only: passing + content-fresh → fresh (no remediation);
      // failing + content-fresh → the verdict is current, so block immediately.
      if (classification.kind === 'stale' && hasContentFingerprint(auditData)) {
        const freshness = queryContentFreshnessFn(item.id);
        if (freshness.fresh) {
          const status = getAuditStatus(item, auditData);
          classification = status.passing
            ? { kind: 'passing', reason: `Content fingerprint unchanged (${freshness.reason})`, summary: classification.summary }
            : { kind: 'failing', reason: `Audit verdict: not ready to close (content-fresh: ${freshness.reason})`, summary: classification.summary };
        }
      }

      summary = classification.summary;

      const needsRemediation = classification.kind === 'missing'
        || classification.kind === 'stale'
        || classification.kind === 'transient';

      if (needsRemediation) {
        // Narrow bypass (SA-0MUOO5WV0006D7UD): do not re-audit in-gate if
        // --skip-audit-remediation was requested; report the item blocking.
        if (skipRemediation) {
          console.log(
            `Final-validation gate: skipping in-gate remediation for ${item.id} `
            + '(--skip-audit-remediation) — blocking.',
          );
          auditIssue = true;
          reasons.push(`${classification.reason} (in-gate remediation skipped)`);
          summary = classification.summary;
        } else if (!budget.canAttempt()) {
          // Bounded remediation (SA-0MUOO5V0P00461X8): once the budget is spent,
          // stop re-auditing and report the item with an offline-refresh
          // instruction instead of implying it is not ready to close.
          console.log(
            `Final-validation gate: remediation budget exhausted `
            + `(${budget.describe()}) — skipping ${item.id}.`,
          );
          auditIssue = true;
          reasons.push(
            `Audit remediation budget exhausted (${budget.describe()}) — `
            + 'audit needs refreshing offline',
          );
        } else {
          console.log(
            `Final-validation gate: auto-remediating ${item.id} ` +
            `(${classification.kind} audit)...`,
          );
          const remediation = await attemptAuditRemediation(item, {
            runAuditShow,
            runAuditCommand,
            resolveAuditRunnerFn,
          });
          budget.record();
          if (remediation.status === 'passing') {
            console.log(`Final-validation gate: ${item.id} auto-remediated successfully.`);
            remediated = true;
            summary = remediation.summary || summary;
          } else {
            console.log(`Final-validation gate: ${item.id} still failing after remediation — blocking.`);
            auditIssue = true;
            reasons.push(remediation.reason);
            summary = remediation.summary || summary;
          }
        }
      } else if (classification.kind === 'failing') {
        auditIssue = true;
        reasons.push(classification.reason);
      }
    }

    if (reasons.length > 0) {
      const remediationParts = [];
      if (producerReview) {
        remediationParts.push(buildProducerReviewRemediationCommand(item.id));
      }
      if (auditIssue) {
        remediationParts.push(
          `${buildRemediationCommand(item.id)}\n  # ${OFFLINE_AUDIT_REFRESH_HINT}`,
        );
      }
      blockingItems.push({
        workItemId: item.id,
        title: item.title,
        reasons,
        reason: reasons.join('; '),
        summary,
        remediation: remediationParts.join('\n'),
      });
    } else if (remediated) {
      remediatedItems.push({
        workItemId: item.id,
        title: item.title,
        reason: 'Audit auto-remediated (re-run passed)',
      });
    } else {
      passingCount++;
    }
  }

  // ── Escalation → parent blocks the gate (SA-0MUJLWPB10038Z8Q) ───────────
  // A child escalated to its parent must fail THIS gate: the producer-review
  // gate (Step 3.6) has already run, so flagging the parent here only takes
  // effect on the next release. To block the current release deterministically
  // the escalated ancestor is surfaced as a blocking item (unless it is
  // already blocking for another reason). Under `--dry-run` nothing was
  // mutated, so the planned escalation must NOT block (AC4).
  const escalatedByAncestor = new Map();
  for (const escalated of escalatedItems) {
    if (escalated.planned) {
      continue;
    }
    if (!escalatedByAncestor.has(escalated.ancestorId)) {
      escalatedByAncestor.set(escalated.ancestorId, []);
    }
    escalatedByAncestor.get(escalated.ancestorId).push(escalated.workItemId);
  }
  for (const [ancestorId, childIds] of escalatedByAncestor) {
    if (blockingItems.some((entry) => entry.workItemId === ancestorId)) {
      continue;
    }
    let ancestorTitle = ancestorId;
    try {
      const ancestor = getItemByIdFn(ancestorId);
      if (ancestor && ancestor.title) {
        ancestorTitle = ancestor.title;
      }
    } catch {
      // Title lookup is cosmetic — fall back to the id.
    }
    const reason = `Escalated producer review from child(ren): ${childIds.join(', ')}`;
    blockingItems.push({
      workItemId: ancestorId,
      title: ancestorTitle,
      reasons: [reason],
      reason,
      summary: null,
      remediation: buildProducerReviewRemediationCommand(ancestorId),
    });
  }

  // ── Build report ───────────────────────────────────────────────────────
  const buildScopeNote = (lines) => {
    if (coveredChildren.length > 0) {
      const byParent = new Map();
      for (const child of coveredChildren) {
        byParent.set(child.parentId, (byParent.get(child.parentId) || 0) + 1);
      }
      lines.push('', 'Child scope — covered by a passing in_review parent audit:', '');
      for (const [parentId, count] of byParent) {
        lines.push(`  - ${count} child(ren) covered by passing parent audit ${parentId}`);
      }
    }
    if (excludedChildren.length > 0) {
      lines.push('', 'Child scope — excluded (out of release scope):', '');
      excludedChildren.forEach((entry, i) => {
        lines.push(
          `${i + 1}. ${entry.title} (${entry.workItemId}) — ` +
          `parent ${entry.parentId} (stage=${entry.parentStage ?? 'unknown'}): ${entry.reason}`,
        );
      });
    }
  };

  const buildRemediatedNote = (lines) => {
    if (remediatedItems.length === 0) {
      return;
    }
    lines.push(
      '',
      `Note: ${remediatedItems.length} work item(s) had missing/stale/transient audits ` +
        'and were auto-remediated (re-audit succeeded, item unblocked):',
      '',
    );
    remediatedItems.forEach((entry, i) => {
      lines.push(`${i + 1}. ${entry.title} (${entry.workItemId})`);
    });
  };

  const buildEscalatedNote = (lines) => {
    if (escalatedItems.length === 0) {
      return;
    }
    const planned = escalatedItems.filter((e) => e.planned);
    if (planned.length > 0) {
      lines.push(
        '',
        `Note (dry-run): ${planned.length} uncovered child(ren) flagged for producer ` +
          'review would be escalated to their nearest in_review ancestor (no mutation performed):',
        '',
      );
      planned.forEach((entry, i) => {
        lines.push(
          `${i + 1}. ${entry.title} (${entry.workItemId}) → would escalate to ${entry.ancestorId}`,
        );
      });
    }
    const actual = escalatedItems.filter((e) => !e.planned);
    if (actual.length > 0) {
      lines.push(
        '',
        `Note: ${actual.length} uncovered child(ren) flagged for producer review ` +
          'were escalated to their nearest in_review ancestor (the parent now blocks ' +
          'the producer-review gate for this release):',
        '',
      );
      actual.forEach((entry, i) => {
        lines.push(
          `${i + 1}. ${entry.title} (${entry.workItemId}) → escalated to ${entry.ancestorId}`,
        );
      });
    }
  };

  const buildPlannedOverridesNote = (lines) => {
    if (plannedOverrides.length === 0) {
      return;
    }
    lines.push(
      '',
      `Note: ${plannedOverrides.length} covered child(ren) flagged for producer ` +
        'review will be overridden at close time (flag cleared) because their nearest ' +
        'in_review ancestor has a passing audit:',
      '',
    );
    plannedOverrides.forEach((entry, i) => {
      lines.push(
        `${i + 1}. ${entry.title} (${entry.workItemId}) → covered by audit of ${entry.ancestorId}`,
      );
    });
  };

  if (blockingItems.length === 0) {
    const lines = [
      `All ${items.length} in_review work item(s) passed final validation ` +
        `(${passingCount} audit-clean, ${remediatedItems.length} auto-remediated, ` +
        `${coveredChildren.length} covered by a passing parent audit, ` +
        `${excludedChildren.length} excluded as out-of-scope). ` +
        'Final-validation gate passed.',
    ];
    buildRemediatedNote(lines);
    buildEscalatedNote(lines);
    buildPlannedOverridesNote(lines);
    buildScopeNote(lines);
    return {
      hasBlockingItems: false,
      blockingItems: [],
      remediatedItems,
      escalatedItems,
      plannedOverrides,
      coveredChildren,
      excludedChildren,
      passingCount,
      dryRun,
      message: lines.join('\n'),
    };
  }

  const lines = [
    `⚠️  Final-validation gate check failed — ${blockingItems.length} of ${items.length} ` +
      'in_review work item(s) have unresolved audit or producer-review issues:',
    '',
  ];

  blockingItems.forEach((entry, i) => {
    lines.push(`${i + 1}. ${entry.title} (${entry.workItemId})`);
    entry.reasons.forEach((reason) => lines.push(`   Reason: ${reason}`));
    if (entry.summary) {
      const summary = entry.summary.length > 200
        ? entry.summary.substring(0, 200) + '...'
        : entry.summary;
      lines.push(`   Summary: ${summary}`);
    }
    lines.push('   Remediation:');
    lines.push(entry.remediation);
    lines.push('');
  });

  buildRemediatedNote(lines);
  buildEscalatedNote(lines);
  buildPlannedOverridesNote(lines);
  buildScopeNote(lines);

  lines.push(
    'Top-level items and uncovered children are validated independently; children',
    'covered by a passing parent audit (or excluded as out-of-scope) are skipped.',
    'After remediation, re-run the release without --skip-checks to re-validate.',
    'Use --skip-checks to bypass this gate.',
  );

  return {
    hasBlockingItems: true,
    blockingItems,
    remediatedItems,
    escalatedItems,
    plannedOverrides,
    coveredChildren,
    excludedChildren,
    passingCount,
    dryRun,
    message: lines.join('\n'),
  };
}
