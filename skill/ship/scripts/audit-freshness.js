/**
 * audit-freshness.js — shared audit-staleness heuristics for the ship skill.
 *
 * Both release gates need to answer the same question: "is this work item's
 * audit stale (i.e. performed before the item was last changed)?" Historically
 * the staleness logic lived only in `check-final-validation.js` (Step 3.7),
 * while `check-audit-gate.js` (Step 2) treated every `readyToClose=false` as a
 * genuine verdict and hard-blocked the release. Because Step 2 runs first, a
 * stale failing verdict killed the release before the staleness-aware sweep
 * could remediate it.
 *
 * Extracting the heuristic here (rather than importing it across the two gate
 * modules) avoids an import cycle: `check-final-validation.js` already imports
 * from `check-audit-gate.js`, so `check-audit-gate.js` cannot import from it.
 *
 * The freshness constants mirror the audit runner
 * (`skill/audit/scripts/audit_runner.py`) so the gate's staleness heuristic
 * matches the runner's own freshness gate. The runner is the source of truth
 * for content-fingerprint freshness; this time gate is the conservative
 * fallback signal — a false "stale" merely triggers a re-run, and the runner
 * short-circuits re-runs of unchanged content via its fingerprint fast path.
 *
 * @module audit-freshness
 */

// Freshness constants mirrored from the audit runner
// (skill/audit/scripts/audit_runner.py) so the staleness heuristic matches
// the runner's own freshness gate. Kept in sync deliberately: the runner is
// the source of truth for freshness; this gate only needs a conservative
// signal that a re-audit is worthwhile (a false "stale" merely triggers the
// runner's fast, content-fingerprint freshness path).
export const AUDIT_FRESHNESS_BUFFER_SECONDS = 60;
export const AUDIT_PERSIST_WRITE_TOLERANCE_SECONDS = 30;

// ── parseIsoUtc ──────────────────────────────────────────────────────────────

/**
 * Parse an ISO-8601 timestamp into a `Date`, treating a missing timezone as
 * UTC. Returns `null` for missing/unparseable values (fail open).
 *
 * @param {string|undefined|null} value - ISO-8601 timestamp.
 * @returns {Date|null}
 */
export function parseIsoUtc(value) {
  if (!value || typeof value !== 'string') {
    return null;
  }
  // Normalise the trailing 'Z' for older runtimes while keeping full
  // ISO-8601 support.
  const normalised = value.replace(/Z$/, '+00:00');
  const withZone = /[+-]\d{2}:?\d{2}$/.test(normalised) ? normalised : `${normalised}+00:00`;
  const parsed = new Date(withZone);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

// ── isAuditStale ─────────────────────────────────────────────────────────────

/**
 * Determine whether an existing audit is stale relative to its work item.
 *
 * Mirrors the audit runner's time-gate floor
 * (`_check_audit_freshness`, skill/audit/scripts/audit_runner.py): an audit
 * is fresh when its `auditedAt` is strictly more than
 * `AUDIT_FRESHNESS_BUFFER_SECONDS` after the item's `updatedAt`, or when the
 * item was updated within `AUDIT_PERSIST_WRITE_TOLERANCE_SECONDS` **after**
 * the audit (the audit's own persistence writes bump `updatedAt`).
 * Everything else is stale.
 *
 * The runner's *primary* freshness signal is a content fingerprint (git
 * HEAD + description hash + Key Files + working tree). This gate cannot
 * recompute that fingerprint cheaply, so it uses the time gate as a
 * conservative signal: a false "stale" only causes a re-run, and the runner
 * short-circuits re-runs of unchanged content via its fingerprint fast path.
 *
 * @param {{ id: string, title: string, updatedAt?: string|null }} workItem -
 *   The work item (must carry `updatedAt` when available).
 * @param {object|null} auditData - Parsed `wl audit-show <id> --json` payload.
 * @returns {boolean} True when the audit exists but is stale; false when no
 *   audit exists, when freshness cannot be determined, or when the audit is
 *   fresh.
 */
export function isAuditStale(workItem, auditData) {
  if (!auditData || !auditData.audit) {
    return false;
  }
  const auditTime = parseIsoUtc(auditData.audit.auditedAt);
  const updateTime = parseIsoUtc(workItem && workItem.updatedAt);
  if (!auditTime || !updateTime) {
    return false; // Cannot determine — fail open, never falsely block.
  }

  const bufferMs = AUDIT_FRESHNESS_BUFFER_SECONDS * 1000;
  if (auditTime.getTime() > updateTime.getTime() + bufferMs) {
    return false; // Audit is newer than the item — fresh.
  }

  const writeDeltaMs = updateTime.getTime() - auditTime.getTime();
  if (writeDeltaMs >= 0 && writeDeltaMs <= AUDIT_PERSIST_WRITE_TOLERANCE_SECONDS * 1000) {
    return false; // The item's own audit-persistence write — fresh.
  }

  return true;
}
