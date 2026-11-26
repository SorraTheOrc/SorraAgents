#!/usr/bin/env node

/**
 * bump-decision.js — Pure version-bump decision rules for the ship skill's
 * "Version bump evaluation" step (SA-0MWFVQX4W0080O86).
 *
 * The judgement itself stays with the agent: it reads the review packet from
 * `bump-preflight.js` and decides patch/minor/major from the shipped items'
 * content. This module only holds the *mechanical* half of the contract so it
 * is testable code rather than prose:
 *
 *   - `shouldAskProducer(bump)` — patch proceeds automatically; minor and
 *     major are approval-pending.
 *   - `buildJustification(...)` — renders the evidence-based proposal the
 *     producer is shown (contributing item ids + titles, the proposed
 *     `old → new` version and the rationale).
 *   - `resolveApproval(...)` — maps a producer decision (or the absence of a
 *     producer, in a headless run) onto the next action.
 *
 * Every function here is **pure**: no `git`, no `wl`, no filesystem access and
 * no subprocess. The approval-pending and declined outcomes never produce a
 * bump to apply, so a non-patch bump can only ever reach the release script
 * after an explicit producer approval (or an operator `--bump` override, which
 * bypasses this module entirely).
 *
 * Usage:
 *
 *   import { shouldAskProducer, resolveApproval, buildJustification }
 *     from './bump-decision.js';
 *
 *   if (shouldAskProducer('minor')) {
 *     console.log(buildJustification({ ... }));
 *     const decision = resolveApproval({ proposedBump: 'minor', producerDecision: answer });
 *   }
 */

// ── Constants ──────────────────────────────────────────────────────────────

/** The three semver bump types, in increasing order of impact. */
export const BUMP_TYPES = Object.freeze(['patch', 'minor', 'major']);

/** The bump applied when nothing else is approved. */
export const DEFAULT_BUMP = 'patch';

/** The bump used when a producer declines a non-patch proposal. */
export const FALLBACK_BUMP = 'patch';

/** Producer answers (lower-cased) that approve a proposed bump. */
const APPROVAL_WORDS = Object.freeze(['approve', 'approved', 'yes', 'y', 'accept', 'accepted', 'ok']);

/** Producer answers (lower-cased) that decline a proposed bump. */
const DECLINE_WORDS = Object.freeze(['decline', 'declined', 'no', 'n', 'reject', 'rejected']);

// ── Bump validation ────────────────────────────────────────────────────────

/**
 * Is *bump* one of the three semver bump types?
 *
 * @param {unknown} bump
 * @returns {boolean}
 */
export function isValidBumpType(bump) {
  return typeof bump === 'string' && BUMP_TYPES.includes(bump);
}

// ── Approval rule ──────────────────────────────────────────────────────────

/**
 * Does this bump require in-session producer approval before it is applied?
 *
 * A `patch` bump is the release default and proceeds automatically; `minor`
 * and `major` change what the version number promises, so the producer always
 * confirms them. An unknown type is treated as approval-pending (fail closed).
 *
 * @param {unknown} bump - Proposed bump type.
 * @returns {boolean} True when the producer must be asked.
 */
export function shouldAskProducer(bump) {
  if (bump === DEFAULT_BUMP) return false;
  return true;
}

// ── Justification rendering ────────────────────────────────────────────────

/**
 * Normalise one contributing item to `{ id, title, reason }`.
 *
 * @param {unknown} entry - An item id, or an object with id/title/reason.
 * @returns {{id: string, title: string, reason: string}}
 */
function normaliseContributing(entry) {
  if (typeof entry === 'string') {
    return { id: entry, title: entry, reason: '' };
  }
  const item = entry && typeof entry === 'object' ? entry : {};
  const id = item.id === undefined || item.id === null ? '' : String(item.id);
  const title = typeof item.title === 'string' && item.title.trim() ? item.title.trim() : id;
  const reason = typeof item.reason === 'string' ? item.reason.trim() : '';
  return { id, title, reason };
}

/**
 * Render the justification shown to the producer for a non-patch proposal.
 *
 * The text names every contributing item (id + title + why it warrants the
 * bump), the proposed `old → new` version, and the evidence behind the call.
 * It is deliberately plain: the agent prints it verbatim, then asks.
 *
 * @param {object} options
 * @param {string} options.proposedBump - Proposed bump type (patch/minor/major).
 * @param {string|null} [options.currentVersion] - Current version (e.g. "0.1.29").
 * @param {string|null} [options.proposedVersion] - Resulting version (e.g. "0.2.0").
 * @param {Array<string|{id: string, title?: string, reason?: string}>} [options.contributing]
 *   The items that justify the bump, each optionally with a reason.
 * @param {string} [options.rationale] - The agent's one-paragraph judgement.
 * @returns {string} Markdown justification (empty string for an invalid bump).
 */
export function buildJustification({
  proposedBump,
  currentVersion = null,
  proposedVersion = null,
  contributing = [],
  rationale = '',
} = {}) {
  if (!isValidBumpType(proposedBump)) return '';

  const items = (Array.isArray(contributing) ? contributing : [])
    .map(normaliseContributing)
    .filter((item) => item.id || item.title);

  const versionLine = currentVersion && proposedVersion
    ? `\`${currentVersion} → ${proposedVersion}\``
    : (proposedVersion ? `→ \`${proposedVersion}\`` : 'unknown');

  const lines = [
    `## Version bump proposal: ${proposedBump} (${versionLine})`,
    '',
    `Producer approval required: ${shouldAskProducer(proposedBump) ? 'yes' : 'no'}`,
    '',
    'Contributing items:',
  ];

  if (items.length === 0) {
    lines.push('- (no individual item identified — the judgement is release-wide)');
  } else {
    for (const item of items) {
      const reason = item.reason ? ` — ${item.reason}` : '';
      lines.push(`- ${item.title} (${item.id})${reason}`);
    }
  }

  const trimmedRationale = typeof rationale === 'string' ? rationale.trim() : '';
  if (trimmedRationale) {
    lines.push('', 'Why this bump:', '', trimmedRationale);
  }

  return `${lines.join('\n').trimEnd()}\n`;
}

// ── Decision resolution ────────────────────────────────────────────────────

/**
 * Normalise a producer answer into `'approved'`, `'declined'` or `null`.
 *
 * Accepts booleans and the common words a producer types in the session, so
 * the workflow does not depend on an exact phrase.
 *
 * @param {unknown} decision
 * @returns {'approved'|'declined'|null}
 */
export function normaliseProducerDecision(decision) {
  if (decision === true) return 'approved';
  if (decision === false) return 'declined';
  if (typeof decision !== 'string') return null;
  const word = decision.trim().toLowerCase();
  if (APPROVAL_WORDS.includes(word)) return 'approved';
  if (DECLINE_WORDS.includes(word)) return 'declined';
  return null;
}

/**
 * Resolve what to do with a proposed bump.
 *
 * Outcomes (`action`):
 *   - `auto-proceed` — a patch bump; apply it without asking anyone.
 *   - `await-approval` — a non-patch bump with no producer answer yet; apply
 *     nothing (`bump: null`) until the producer answers.
 *   - `proceed` — the producer approved; apply `bump` (the approved type).
 *   - `declined` — the producer declined; apply nothing (`bump: null`) and use
 *     `fallbackBump` instead.
 *   - `await-producer-review` — headless run with no producer available; apply
 *     nothing, flag the release for producer review and stop.
 *   - `invalid` — the proposal is not a valid bump type; apply nothing.
 *
 * @param {object} options
 * @param {unknown} options.proposedBump - Proposed bump type.
 * @param {unknown} [options.producerDecision] - The producer's in-session answer.
 * @param {boolean} [options.headless=false] - True when no producer can be asked.
 * @returns {{action: string, bump: string|null, requiresProducerApproval: boolean,
 *   fallbackBump: string|null, reason: string}}
 */
export function resolveApproval({
  proposedBump,
  producerDecision = null,
  headless = false,
} = {}) {
  if (!isValidBumpType(proposedBump)) {
    return {
      action: 'invalid',
      bump: null,
      requiresProducerApproval: true,
      fallbackBump: null,
      reason: `Invalid bump type "${proposedBump}". Expected one of: ${BUMP_TYPES.join(', ')}.`,
    };
  }

  if (!shouldAskProducer(proposedBump)) {
    return {
      action: 'auto-proceed',
      bump: DEFAULT_BUMP,
      requiresProducerApproval: false,
      fallbackBump: null,
      reason: 'A patch bump proceeds automatically; no producer interaction is required.',
    };
  }

  const answer = normaliseProducerDecision(producerDecision);

  if (answer === 'approved') {
    return {
      action: 'proceed',
      bump: proposedBump,
      requiresProducerApproval: false,
      fallbackBump: null,
      reason: `The producer approved the ${proposedBump} bump.`,
    };
  }

  if (answer === 'declined') {
    return {
      action: 'declined',
      bump: null,
      requiresProducerApproval: false,
      fallbackBump: FALLBACK_BUMP,
      reason: `The producer declined the ${proposedBump} bump; use ${FALLBACK_BUMP} instead.`,
    };
  }

  if (headless) {
    return {
      action: 'await-producer-review',
      bump: null,
      requiresProducerApproval: true,
      fallbackBump: null,
      reason: `A ${proposedBump} bump needs producer approval and no producer is available `
        + 'in this headless run — record the justification, flag the release for producer review and stop.',
    };
  }

  return {
    action: 'await-approval',
    bump: null,
    requiresProducerApproval: true,
    fallbackBump: null,
    reason: `Ask the producer in this session and apply nothing until the ${proposedBump} bump is approved.`,
  };
}
