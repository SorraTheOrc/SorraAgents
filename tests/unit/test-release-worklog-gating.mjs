/**
 * Worklog-ref gating in the release pipeline — structural contracts.
 *
 * The *behaviour* of the worklog-ref gate (which refs are dangerous, which are
 * legitimate `wl sync` bookkeeping, and parity between the shell and JS
 * implementations) is covered by real behavioural tests in
 * `test-worklog-ref-gate.mjs`. This file only asserts the wiring that those
 * tests cannot observe from outside the release script: that
 * `merge-dev-to-main.sh` delegates to the shared gate rather than carrying its
 * own copy of the rule (the divergence that broke every release —
 * SA-0MVO7XZAB0095R6F), and that the gate runs before the merge.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');

const MERGE_SCRIPT_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'release', 'merge-dev-to-main.sh');
const RUN_RELEASE_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');

// ── merge-dev-to-main.sh worklog-ref gating ─────────────────────────────────

describe('merge-dev-to-main.sh: worklog-ref gating delegates to the shared gate', () => {
  const content = readFileSync(MERGE_SCRIPT_PATH, 'utf-8');

  test('invokes the extracted worklog-ref gate', () => {
    assert.match(
      content,
      /worklog-ref-gate\.sh/,
      'merge-dev-to-main.sh must call release/worklog-ref-gate.sh instead of inlining the rule',
    );
  });

  test('does not re-inline its own ref-name special case', () => {
    // The previous bug: an inline gate that only special-cased the literal name
    // `refs/worklog/data`, so `wl sync` bookkeeping refs
    // (refs/worklog/fetched-check, refs/worklog/tmp-reauthor) aborted every
    // release while the JS gate passed. The rule must live in one place.
    assert.doesNotMatch(
      content,
      /for-each-ref\s+refs\/worklog\//,
      'the ref enumeration belongs in worklog-ref-gate.sh, not in the merge script',
    );
  });

  test('aborts with exit 1 and a stderr explanation when the gate fails', () => {
    assert.match(content, /Dangerous worklog refs detected/, 'must keep the operator-facing abort message');
    // Slice from the actual invocation (not the explanatory comment above it).
    const callPos = content.indexOf('$SCRIPT_DIR/worklog-ref-gate.sh');
    assert.ok(callPos >= 0, 'the gate must be invoked via $SCRIPT_DIR/worklog-ref-gate.sh');
    const gateBlock = content.slice(callPos, callPos + 600);
    assert.match(gateBlock, /exit 1/, 'a failing gate must abort the release');
    assert.match(gateBlock, />&2/, 'the abort explanation must go to stderr');
  });

  test('gating check runs before the merge operation', () => {
    const gatePos = content.indexOf('$SCRIPT_DIR/worklog-ref-gate.sh');
    const mergePos = content.indexOf('git merge');
    assert.ok(gatePos >= 0 && mergePos >= 0, 'both the gate call and the merge operation must exist');
    assert.ok(gatePos < mergePos, 'the worklog-ref gate must run before the merge');
  });
});

// ── run-release.js worklog-ref gating ────────────────────────────────────────

describe('run-release.js: worklog-ref gating', () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');

  test('references the worklog-ref gate (wrapper-level check)', () => {
    assert.match(
      content,
      /checkWorklogRefs/,
      'run-release.js should apply the JS worklog-ref gate before the merge',
    );
  });
});

// ── Integration: the shell script has proper error handling ─────────────────

describe('merge-dev-to-main.sh: error handling structure', () => {
  const content = readFileSync(MERGE_SCRIPT_PATH, 'utf-8');

  test('gating check is inside a bash if/fi block', () => {
    assert.ok(
      content.includes('if') && content.includes('fi'),
      'The gating check should use proper bash conditionals',
    );
  });

  test('error output goes to stderr', () => {
    assert.ok(
      content.includes('>&2') || content.includes('&>2') || content.includes('1>&2'),
      'Error messages should be directed to stderr',
    );
  });
});
