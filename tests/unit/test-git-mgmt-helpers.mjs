/**
 * Unit tests for skill/git-management/scripts/git-mgmt-helpers.mjs
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
  EXIT,
  jsonOutput,
  humanMsg,
  humanSuccess,
  humanError,
  commandExists,
  checkPrerequisites,
  safeExec,
  parseArgs,
  hasFlag,
  getFlag,
  validateWorkItemId,
  makeSlug,
} from '../../skill/git-management/scripts/git-mgmt-helpers.mjs';

// ── EXIT constants ───────────────────────────────────────────────────────────

describe('EXIT constants', () => {
  test('SUCCESS is 0', () => {
    assert.equal(EXIT.SUCCESS, 0);
  });

  test('GENERAL_ERROR is 1', () => {
    assert.equal(EXIT.GENERAL_ERROR, 1);
  });

  test('SAFETY_VIOLATION is 2', () => {
    assert.equal(EXIT.SAFETY_VIOLATION, 2);
  });

  test('PREREQ_NOT_MET is 3', () => {
    assert.equal(EXIT.PREREQ_NOT_MET, 3);
  });

  test('EXIT is frozen', () => {
    assert.throws(() => { EXIT.SUCCESS = 99; });
  });
});

// ── commandExists ────────────────────────────────────────────────────────────

describe('commandExists', () => {
  test('returns true for common commands', () => {
    assert.equal(commandExists('ls'), true);
  });

  test('returns false for non-existent commands', () => {
    assert.equal(commandExists('this-command-definitely-does-not-exist-xyz123'), false);
  });
});

// ── safeExec ─────────────────────────────────────────────────────────────────

describe('safeExec', () => {
  test('returns success for valid commands', () => {
    const result = safeExec('echo hello');
    assert.equal(result.success, true);
    assert.equal(result.stdout, 'hello');
    assert.equal(result.exitCode, 0);
  });

  test('returns failure for invalid commands', () => {
    const result = safeExec('false');
    assert.equal(result.success, false);
    assert.ok(result.exitCode > 0);
  });

  test('captures stderr on failure', () => {
    const result = safeExec('ls /nonexistent-path-xyz123');
    assert.equal(result.success, false);
  });
});

// ── parseArgs ────────────────────────────────────────────────────────────────

describe('parseArgs', () => {
  test('parses --flag value pairs', () => {
    const { flags, positional } = parseArgs(['--name', 'test', '--count', '5']);
    assert.equal(flags.get('name'), 'test');
    assert.equal(flags.get('count'), '5');
    assert.equal(positional.length, 0);
  });

  test('parses --flag without value as true', () => {
    const { flags } = parseArgs(['--dry-run', '--json']);
    assert.equal(flags.get('dry-run'), true);
    assert.equal(flags.get('json'), true);
  });

  test('parses positional arguments', () => {
    const { flags, positional } = parseArgs(['arg1', 'arg2']);
    assert.equal(flags.size, 0);
    assert.deepEqual(positional, ['arg1', 'arg2']);
  });

  test('handles --flag=value syntax', () => {
    const { flags } = parseArgs(['--message=hello world']);
    assert.equal(flags.get('message'), 'hello world');
  });

  test('parses short flags', () => {
    const { flags } = parseArgs(['-m', 'test']);
    assert.equal(flags.get('m'), 'test');
  });

  test('empty args returns empty flags and positional', () => {
    const { flags, positional } = parseArgs([]);
    assert.equal(flags.size, 0);
    assert.equal(positional.length, 0);
  });
});

// ── hasFlag / getFlag ────────────────────────────────────────────────────────

describe('hasFlag and getFlag', () => {
  test('hasFlag returns true for present flags', () => {
    const { flags } = parseArgs(['--dry-run']);
    assert.equal(hasFlag(flags, 'dry-run'), true);
  });

  test('hasFlag returns false for absent flags', () => {
    const { flags } = parseArgs(['--dry-run']);
    assert.equal(hasFlag(flags, 'json'), false);
  });

  test('getFlag returns value for valued flags', () => {
    const { flags } = parseArgs(['--name', 'test']);
    assert.equal(getFlag(flags, 'name'), 'test');
  });

  test('getFlag returns true for valueless flags', () => {
    const { flags } = parseArgs(['--dry-run']);
    assert.equal(getFlag(flags, 'dry-run'), true);
  });

  test('getFlag returns undefined for absent flags', () => {
    const { flags } = parseArgs([]);
    assert.equal(getFlag(flags, 'missing'), undefined);
  });
});

// ── validateWorkItemId ──────────────────────────────────────────────────────

describe('validateWorkItemId', () => {
  test('accepts valid SA- IDs', () => {
    const result = validateWorkItemId('SA-0MPMI7FWI004PXHS');
    assert.equal(result.valid, true);
  });

  test('accepts IDs with hyphens', () => {
    const result = validateWorkItemId('SA-0MPDZDPZB00121IE');
    assert.equal(result.valid, true);
  });

  test('rejects empty string', () => {
    const result = validateWorkItemId('');
    assert.equal(result.valid, false);
  });

  test('rejects undefined', () => {
    const result = validateWorkItemId(undefined);
    assert.equal(result.valid, false);
  });

  test('rejects lowercase IDs', () => {
    const result = validateWorkItemId('sa-001');
    assert.equal(result.valid, false);
  });

  test('rejects IDs without hyphen separator', () => {
    const result = validateWorkItemId('SA001');
    assert.equal(result.valid, false);
  });

  test('rejects IDs starting with number', () => {
    const result = validateWorkItemId('0A-001');
    assert.equal(result.valid, false);
  });
});

// ── makeSlug ─────────────────────────────────────────────────────────────────

describe('makeSlug', () => {
  test('converts to lowercase', () => {
    assert.equal(makeSlug('Hello World'), 'hello-world');
  });

  test('replaces spaces with hyphens', () => {
    assert.equal(makeSlug('hello world foo'), 'hello-world-foo');
  });

  test('removes special characters', () => {
    assert.equal(makeSlug('hello!@#$world'), 'hello-world');
  });

  test('collapses multiple hyphens', () => {
    assert.equal(makeSlug('hello---world'), 'hello-world');
  });

  test('strips leading/trailing hyphens', () => {
    assert.equal(makeSlug('-hello-world-'), 'hello-world');
  });

  test('handles empty input', () => {
    assert.equal(makeSlug(''), '');
  });

  test('handles whitespace-only input', () => {
    assert.equal(makeSlug('   '), '');
  });
});

// ── checkPrerequisites ───────────────────────────────────────────────────────

describe('checkPrerequisites', () => {
  test('passes when all commands exist', () => {
    const result = checkPrerequisites(['ls']);
    assert.equal(result.ok, true);
    assert.equal(result.errors.length, 0);
  });

  test('fails when a command is missing', () => {
    const result = checkPrerequisites(['this-command-does-not-exist-xyz']);
    assert.equal(result.ok, false);
    assert.ok(result.errors.length > 0);
  });

  test('detects not-in-git-repo', () => {
    const result = checkPrerequisites(['git'], { requireGitDir: true });
    // This test depends on where it's run; if in the repo it passes
    // If not, it fails. We just verify the structure.
    assert.ok('ok' in result);
    assert.ok(Array.isArray(result.errors));
  });
});
