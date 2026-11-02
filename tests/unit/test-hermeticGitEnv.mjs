/**
 * Regression pin for the hermetic fixture-git environment helper
 * (SA-0MVYS34QF000AWQX).
 *
 * `implement.py finish` exports a temporary `GIT_INDEX_FILE` for the suite it
 * runs (SA-0MVQHVGFR006W8LG) and the test-skill scrub deliberately preserves
 * it (F3, SA-0MVQHV9Y3001RNNW). Node tests that build fixture git repositories
 * must never inherit it — a fixture's git writes would land in the gate's
 * index (failing the tests and invalidating the gate's tree evidence). This
 * test pins the scrub contract: the helper's output is a copy of the base
 * environment with repository-override variables and `GIT_INDEX_FILE`
 * removed, and the base is never mutated.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { hermeticGitEnv } from '../helpers/hermeticGitEnv.mjs';

const OVERRIDE_VARS = [
  'GIT_DIR',
  'GIT_WORK_TREE',
  'GIT_CONFIG',
  'GIT_CONFIG_GLOBAL',
  'GIT_CONFIG_SYSTEM',
  'GIT_CONFIG_NOSYSTEM',
  'GIT_OBJECT_DIRECTORY',
  'GIT_ALTERNATE_OBJECT_DIRECTORIES',
  'GIT_INDEX_FILE',
];

describe('hermeticGitEnv — fixture git environments are hermetic (SA-0MVYS34QF000AWQX)', () => {
  test('drops GIT_INDEX_FILE and every repository-override var', () => {
    const base = {
      GIT_INDEX_FILE: '/tmp/gate-index',
      GIT_DIR: '/tmp/gate-git',
      GIT_WORK_TREE: '/tmp/gate-wt',
      GIT_CONFIG: '/tmp/cfg',
      GIT_CONFIG_GLOBAL: '/tmp/global',
      GIT_CONFIG_SYSTEM: '/tmp/system',
      GIT_CONFIG_NOSYSTEM: '1',
      GIT_OBJECT_DIRECTORY: '/tmp/obj',
      GIT_ALTERNATE_OBJECT_DIRECTORIES: '/tmp/alt',
      KEEP_ME: 'yes',
      PATH: '/usr/bin',
    };
    const env = hermeticGitEnv(base);
    for (const name of OVERRIDE_VARS) {
      assert.equal(env[name], undefined, `${name} must be scrubbed`);
    }
    assert.equal(env.KEEP_ME, 'yes');
    assert.equal(env.PATH, '/usr/bin');
  });

  test('returns a copy and never mutates the base environment', () => {
    const base = { GIT_INDEX_FILE: 'x', KEEP: '1' };
    const env = hermeticGitEnv(base);
    assert.equal(env.GIT_INDEX_FILE, undefined);
    assert.equal(base.GIT_INDEX_FILE, 'x'); // base untouched
    assert.equal(base.KEEP, '1');
    const procs = hermeticGitEnv();
    assert.notEqual(procs, process.env); // a copy, not the live environ
  });

  test('callers can layer deterministic overrides on top', () => {
    const env = {
      ...hermeticGitEnv({ GIT_INDEX_FILE: 'inherited' }),
      GIT_CONFIG_GLOBAL: '/dev/null',
      GIT_AUTHOR_NAME: 'Test',
    };
    assert.equal(env.GIT_INDEX_FILE, undefined);
    assert.equal(env.GIT_CONFIG_GLOBAL, '/dev/null');
    assert.equal(env.GIT_AUTHOR_NAME, 'Test');
  });
});
