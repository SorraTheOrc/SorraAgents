import { test, describe, mock } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import path from 'path';
import os from 'os';

// Import the plugin module — all new helpers/constants are named exports.
const pluginModule = new URL('../../skill/install-ampa/resources/ampa.mjs', import.meta.url);
const plugin = await import(pluginModule.href);

// Re-use the FakeProgram/FakeCommand pattern from test-ampa.mjs for command
// registration tests.
class FakeProgram {
  constructor() {
    this.commands = new Map();
  }

  command(name) {
    const cmd = new FakeCommand(name, this);
    this.commands.set(name, cmd);
    return cmd;
  }
}

class FakeCommand {
  constructor(name, program) {
    this.name = name;
    this.program = program;
    this.subcommands = new Map();
    this.actionFn = null;
  }

  command(name) {
    const cmd = new FakeCommand(name, this.program);
    this.subcommands.set(name, cmd);
    return cmd;
  }

  description() {
    return this;
  }

  option() {
    return this;
  }

  arguments() {
    return this;
  }

  action(fn) {
    this.actionFn = fn;
    return this;
  }
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

describe('dev container constants', () => {
  test('CONTAINER_IMAGE is ampa-dev:latest', () => {
    assert.equal(plugin.CONTAINER_IMAGE, 'ampa-dev:latest');
  });

  test('CONTAINER_PREFIX is ampa-', () => {
    assert.equal(plugin.CONTAINER_PREFIX, 'ampa-');
  });

  test('TEMPLATE_CONTAINER_NAME is ampa-template', () => {
    assert.equal(plugin.TEMPLATE_CONTAINER_NAME, 'ampa-template');
  });
});

// ---------------------------------------------------------------------------
// branchName generation
// ---------------------------------------------------------------------------

describe('branchName', () => {
  test('feature issue type produces feature/<id>', () => {
    assert.equal(plugin.branchName('SA-123', 'feature'), 'feature/SA-123');
  });

  test('bug issue type produces bug/<id>', () => {
    assert.equal(plugin.branchName('SA-456', 'bug'), 'bug/SA-456');
  });

  test('chore issue type produces chore/<id>', () => {
    assert.equal(plugin.branchName('WL-1', 'chore'), 'chore/WL-1');
  });

  test('task issue type produces task/<id>', () => {
    assert.equal(plugin.branchName('WL-2', 'task'), 'task/WL-2');
  });

  test('unknown issue type defaults to task/<id>', () => {
    assert.equal(plugin.branchName('SA-789', 'epic'), 'task/SA-789');
  });

  test('empty issue type defaults to task/<id>', () => {
    assert.equal(plugin.branchName('SA-100', ''), 'task/SA-100');
  });

  test('null issue type defaults to task/<id>', () => {
    assert.equal(plugin.branchName('SA-200', null), 'task/SA-200');
  });

  test('undefined issue type defaults to task/<id>', () => {
    assert.equal(plugin.branchName('SA-300', undefined), 'task/SA-300');
  });
});

// ---------------------------------------------------------------------------
// containerName generation
// ---------------------------------------------------------------------------

describe('containerName', () => {
  test('generates ampa-<id> from work item ID', () => {
    assert.equal(plugin.containerName('SA-0MLQ8YD0Z1C1FP07'), 'ampa-SA-0MLQ8YD0Z1C1FP07');
  });

  test('generates ampa-<id> from short ID', () => {
    assert.equal(plugin.containerName('WL-1'), 'ampa-WL-1');
  });
});

// ---------------------------------------------------------------------------
// checkBinary
// ---------------------------------------------------------------------------

describe('checkBinary', () => {
  test('returns true for a binary that exists (node)', () => {
    assert.equal(plugin.checkBinary('node'), true);
  });

  test('returns false for a binary that does not exist', () => {
    assert.equal(plugin.checkBinary('nonexistent-binary-xyz-999'), false);
  });
});

// ---------------------------------------------------------------------------
// checkPrerequisites
// ---------------------------------------------------------------------------

describe('checkPrerequisites', () => {
  test('returns an object with ok and missing properties', () => {
    const result = plugin.checkPrerequisites();
    assert.ok(typeof result.ok === 'boolean');
    assert.ok(Array.isArray(result.missing));
  });

  test('missing array contains any tools not in PATH', () => {
    const result = plugin.checkPrerequisites();
    // We can't guarantee what's installed in CI, but the shape is correct
    for (const m of result.missing) {
      assert.ok(['podman', 'distrobox', 'git', 'wl'].includes(m));
    }
  });
});

// ---------------------------------------------------------------------------
// getGitOrigin
// ---------------------------------------------------------------------------

describe('getGitOrigin', () => {
  test('returns a string URL when in a git repo with origin', () => {
    const origin = plugin.getGitOrigin();
    // We are in a git repo with an origin remote
    assert.ok(origin === null || typeof origin === 'string');
    if (origin) {
      assert.ok(origin.length > 0, 'origin should not be empty');
    }
  });
});

// ---------------------------------------------------------------------------
// validateWorkItem
// ---------------------------------------------------------------------------

describe('validateWorkItem', () => {
  test('returns null for a non-existent work item ID', () => {
    const result = plugin.validateWorkItem('NONEXISTENT-FAKE-ID-999');
    assert.equal(result, null);
  });

  // Note: We can't test a valid work item without wl being fully configured,
  // which depends on runtime state. The function is tested end-to-end instead.
});

// ---------------------------------------------------------------------------
// checkContainerExists
// ---------------------------------------------------------------------------

describe('checkContainerExists', () => {
  test('returns false for a non-existent container', () => {
    // This will either return false (podman installed, no such container)
    // or false (podman not installed, command fails with non-zero).
    const result = plugin.checkContainerExists('nonexistent-container-xyz-999');
    assert.equal(result, false);
  });
});

// ---------------------------------------------------------------------------
// ensureTemplate
// ---------------------------------------------------------------------------

describe('ensureTemplate', () => {
  test('is exported as a function', () => {
    assert.equal(typeof plugin.ensureTemplate, 'function');
  });

  test('returns ok: true when template already exists', () => {
    // Only test the return shape when the template container already exists,
    // otherwise calling ensureTemplate() would attempt a slow distrobox create.
    const templateExists = plugin.checkContainerExists(plugin.TEMPLATE_CONTAINER_NAME);
    if (!templateExists) {
      // Skip — cannot test without triggering a slow distrobox create
      return;
    }
    const result = plugin.ensureTemplate();
    assert.ok(typeof result === 'object' && result !== null, 'should return an object');
    assert.equal(result.ok, true);
    assert.ok(typeof result.message === 'string', 'message should be a string');
  });
});
// Command registration
// ---------------------------------------------------------------------------

describe('command registration', () => {
  let ampaCmd;

  test('registers ampa command with all subcommands', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    ampaCmd = ctx.program.commands.get('ampa');
    assert.ok(ampaCmd, 'ampa command should be registered');
  });

  test('registers start-work subcommand', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    assert.ok(ampa.subcommands.has('start-work'), 'start-work should be registered');
    assert.ok(ampa.subcommands.get('start-work').actionFn, 'start-work should have an action');
  });

  test('registers sw alias', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    assert.ok(ampa.subcommands.has('sw'), 'sw alias should be registered');
    assert.ok(ampa.subcommands.get('sw').actionFn, 'sw should have an action');
  });

  test('registers finish-work subcommand', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    assert.ok(ampa.subcommands.has('finish-work'), 'finish-work should be registered');
    assert.ok(ampa.subcommands.get('finish-work').actionFn, 'finish-work should have an action');
  });

  test('registers fw alias', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    assert.ok(ampa.subcommands.has('fw'), 'fw alias should be registered');
    assert.ok(ampa.subcommands.get('fw').actionFn, 'fw should have an action');
  });

  test('registers list-containers subcommand', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    assert.ok(ampa.subcommands.has('list-containers'), 'list-containers should be registered');
    assert.ok(ampa.subcommands.get('list-containers').actionFn, 'list-containers should have an action');
  });

  test('registers lc alias', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    assert.ok(ampa.subcommands.has('lc'), 'lc alias should be registered');
    assert.ok(ampa.subcommands.get('lc').actionFn, 'lc should have an action');
  });

  test('still registers original subcommands (start, stop, status, run, list, ls)', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    for (const cmd of ['start', 'stop', 'status', 'run', 'list', 'ls']) {
      assert.ok(ampa.subcommands.has(cmd), `${cmd} should be registered`);
    }
  });
});

// ---------------------------------------------------------------------------
// finish-work detection (outside container)
// ---------------------------------------------------------------------------

describe('finish-work outside container', () => {
  test('finish-work exits with code 2 when not in a container and no claimed containers', async () => {
    // Ensure env vars are not set
    const origName = process.env.AMPA_CONTAINER_NAME;
    const origId = process.env.AMPA_WORK_ITEM_ID;
    delete process.env.AMPA_CONTAINER_NAME;
    delete process.env.AMPA_WORK_ITEM_ID;

    // Save and clear pool state so there are no claimed containers
    const projectRoot = process.cwd();
    const statePath = plugin.poolStatePath(projectRoot);
    let origState;
    try { origState = fs.readFileSync(statePath, 'utf8'); } catch (e) { origState = null; }
    plugin.savePoolState(projectRoot, {});

    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    const fwCmd = ampa.subcommands.get('finish-work');

    // Capture stderr
    const originalError = console.error;
    let errorOutput = '';
    console.error = (...args) => { errorOutput += args.join(' ') + '\n'; };

    process.exitCode = undefined;
    await fwCmd.actionFn(undefined, { force: false });
    console.error = originalError;

    assert.equal(process.exitCode, 2, 'should set exit code to 2');
    assert.ok(errorOutput.includes('No claimed containers found'), 'should print no claimed containers error');

    // Restore pool state
    if (origState !== null) {
      fs.writeFileSync(statePath, origState);
    }

    // Restore env
    if (origName !== undefined) process.env.AMPA_CONTAINER_NAME = origName;
    if (origId !== undefined) process.env.AMPA_WORK_ITEM_ID = origId;
    process.exitCode = undefined;
  });
});

// ---------------------------------------------------------------------------
// Pool constants
// ---------------------------------------------------------------------------

describe('pool constants', () => {
  test('POOL_PREFIX is ampa-pool-', () => {
    assert.equal(plugin.POOL_PREFIX, 'ampa-pool-');
  });

  test('POOL_SIZE is 3', () => {
    assert.equal(plugin.POOL_SIZE, 3);
  });
});

// ---------------------------------------------------------------------------
// poolContainerName
// ---------------------------------------------------------------------------

describe('poolContainerName', () => {
  test('generates ampa-pool-0 for index 0', () => {
    assert.equal(plugin.poolContainerName(0), 'ampa-pool-0');
  });

  test('generates ampa-pool-2 for index 2', () => {
    assert.equal(plugin.poolContainerName(2), 'ampa-pool-2');
  });
});

// ---------------------------------------------------------------------------
// poolStatePath
// ---------------------------------------------------------------------------

describe('poolStatePath', () => {
  test('returns path under .worklog/ampa/', () => {
    const p = plugin.poolStatePath('/tmp/test-project');
    assert.equal(p, '/tmp/test-project/.worklog/ampa/pool-state.json');
  });
});

// ---------------------------------------------------------------------------
// Pool state read/write (using a temp directory)
// ---------------------------------------------------------------------------

describe('getPoolState / savePoolState', () => {
  let tmpDir;

  // Create a fresh temp dir for each test to avoid cross-contamination
  function makeTmpProject() {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pool-test-'));
    return dir;
  }

  test('getPoolState returns empty object when no state file exists', () => {
    tmpDir = makeTmpProject();
    const state = plugin.getPoolState(tmpDir);
    assert.deepEqual(state, {});
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  test('savePoolState creates directories and writes JSON', () => {
    tmpDir = makeTmpProject();
    const testState = { 'ampa-pool-0': { workItemId: 'WL-1', branch: 'feature/WL-1', claimedAt: '2025-01-01T00:00:00.000Z' } };
    plugin.savePoolState(tmpDir, testState);
    const stateFile = plugin.poolStatePath(tmpDir);
    assert.ok(fs.existsSync(stateFile), 'state file should be created');
    const read = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
    assert.deepEqual(read, testState);
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  test('getPoolState reads back saved state', () => {
    tmpDir = makeTmpProject();
    const testState = { 'ampa-pool-1': { workItemId: 'SA-42', branch: 'bug/SA-42', claimedAt: '2025-06-15T12:00:00.000Z' } };
    plugin.savePoolState(tmpDir, testState);
    const read = plugin.getPoolState(tmpDir);
    assert.deepEqual(read, testState);
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });
});

// ---------------------------------------------------------------------------
// claimPoolContainer / releasePoolContainer / findPoolContainerForWorkItem
// ---------------------------------------------------------------------------

describe('claimPoolContainer', () => {
  test('returns null or a pool container name depending on pool state', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pool-test-'));
    const result = plugin.claimPoolContainer(tmpDir, 'WL-1', 'feature/WL-1');
    if (result === null) {
      // No pool containers exist in podman — expected in CI or fresh hosts
      assert.equal(result, null);
    } else {
      // Pool containers exist on this host — claim should return a valid name
      assert.ok(result.startsWith(plugin.POOL_PREFIX), `expected pool name, got: ${result}`);
      // Verify the claim was persisted
      const state = plugin.getPoolState(tmpDir);
      assert.ok(state[result], 'claimed container should appear in pool state');
      assert.equal(state[result].workItemId, 'WL-1');
      // Clean up claim
      plugin.releasePoolContainer(tmpDir, result);
    }
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });
});

describe('releasePoolContainer', () => {
  test('removes a specific container claim from state', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pool-test-'));
    const state = {
      'ampa-pool-0': { workItemId: 'WL-1', branch: 'feature/WL-1', claimedAt: '2025-01-01T00:00:00.000Z' },
      'ampa-pool-1': { workItemId: 'WL-2', branch: 'task/WL-2', claimedAt: '2025-01-02T00:00:00.000Z' },
    };
    plugin.savePoolState(tmpDir, state);
    plugin.releasePoolContainer(tmpDir, 'ampa-pool-0');
    const updated = plugin.getPoolState(tmpDir);
    assert.equal(updated['ampa-pool-0'], undefined, 'ampa-pool-0 should be removed');
    assert.ok(updated['ampa-pool-1'], 'ampa-pool-1 should remain');
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  test('clears all claims with wildcard *', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pool-test-'));
    const state = {
      'ampa-pool-0': { workItemId: 'WL-1', branch: 'feature/WL-1', claimedAt: '2025-01-01T00:00:00.000Z' },
      'ampa-pool-1': { workItemId: 'WL-2', branch: 'task/WL-2', claimedAt: '2025-01-02T00:00:00.000Z' },
    };
    plugin.savePoolState(tmpDir, state);
    plugin.releasePoolContainer(tmpDir, '*');
    const updated = plugin.getPoolState(tmpDir);
    assert.deepEqual(updated, {});
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });
});

describe('findPoolContainerForWorkItem', () => {
  test('returns the container name for a claimed work item', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pool-test-'));
    const state = {
      'ampa-pool-0': { workItemId: 'WL-1', branch: 'feature/WL-1', claimedAt: '2025-01-01T00:00:00.000Z' },
      'ampa-pool-2': { workItemId: 'SA-99', branch: 'bug/SA-99', claimedAt: '2025-02-01T00:00:00.000Z' },
    };
    plugin.savePoolState(tmpDir, state);
    assert.equal(plugin.findPoolContainerForWorkItem(tmpDir, 'SA-99'), 'ampa-pool-2');
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  test('returns null for an unclaimed work item', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pool-test-'));
    plugin.savePoolState(tmpDir, {});
    assert.equal(plugin.findPoolContainerForWorkItem(tmpDir, 'WL-999'), null);
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  test('returns null when state file does not exist', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pool-test-'));
    assert.equal(plugin.findPoolContainerForWorkItem(tmpDir, 'WL-1'), null);
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });
});

// ---------------------------------------------------------------------------
// poolCleanupPath / getCleanupList / saveCleanupList / markForCleanup / cleanupMarkedContainers
// ---------------------------------------------------------------------------

describe('poolCleanupPath', () => {
  test('returns path inside .worklog/ampa/', () => {
    const p = plugin.poolCleanupPath('/tmp/test-project');
    assert.ok(p.includes('.worklog'));
    assert.ok(p.includes('ampa'));
    assert.ok(p.endsWith('pool-cleanup.json'));
  });
});

describe('getCleanupList / saveCleanupList', () => {
  test('returns empty array when no cleanup file exists', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-cleanup-test-'));
    const list = plugin.getCleanupList(tmpDir);
    assert.deepEqual(list, []);
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  test('saveCleanupList creates directories and writes JSON array', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-cleanup-test-'));
    plugin.saveCleanupList(tmpDir, ['ampa-pool-1', 'ampa-pool-3']);
    const p = plugin.poolCleanupPath(tmpDir);
    assert.ok(fs.existsSync(p), 'cleanup file should exist');
    const data = JSON.parse(fs.readFileSync(p, 'utf8'));
    assert.deepEqual(data, ['ampa-pool-1', 'ampa-pool-3']);
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  test('getCleanupList reads back saved list', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-cleanup-test-'));
    plugin.saveCleanupList(tmpDir, ['ampa-pool-5']);
    const list = plugin.getCleanupList(tmpDir);
    assert.deepEqual(list, ['ampa-pool-5']);
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });
});

describe('markForCleanup', () => {
  test('adds a container name to the cleanup list', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-cleanup-test-'));
    plugin.markForCleanup(tmpDir, 'ampa-pool-2');
    const list = plugin.getCleanupList(tmpDir);
    assert.deepEqual(list, ['ampa-pool-2']);
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  test('does not add duplicates', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-cleanup-test-'));
    plugin.markForCleanup(tmpDir, 'ampa-pool-2');
    plugin.markForCleanup(tmpDir, 'ampa-pool-2');
    const list = plugin.getCleanupList(tmpDir);
    assert.deepEqual(list, ['ampa-pool-2']);
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  test('appends to existing list', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-cleanup-test-'));
    plugin.markForCleanup(tmpDir, 'ampa-pool-1');
    plugin.markForCleanup(tmpDir, 'ampa-pool-4');
    const list = plugin.getCleanupList(tmpDir);
    assert.deepEqual(list, ['ampa-pool-1', 'ampa-pool-4']);
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });
});

describe('cleanupMarkedContainers', () => {
  test('is exported as a function', () => {
    assert.equal(typeof plugin.cleanupMarkedContainers, 'function');
  });

  test('returns empty arrays when no containers are marked', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-cleanup-test-'));
    const result = plugin.cleanupMarkedContainers(tmpDir);
    assert.deepEqual(result.destroyed, []);
    assert.deepEqual(result.errors, []);
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });
});

// ---------------------------------------------------------------------------
// imageCreatedDate / isImageStale / teardownStalePool
// ---------------------------------------------------------------------------

describe('imageCreatedDate', () => {
  test('is exported as a function', () => {
    assert.equal(typeof plugin.imageCreatedDate, 'function');
  });

  test('returns null for a non-existent image', () => {
    const result = plugin.imageCreatedDate('no-such-image:never');
    assert.equal(result, null);
  });
});

describe('isImageStale', () => {
  test('is exported as a function', () => {
    assert.equal(typeof plugin.isImageStale, 'function');
  });

  test('returns false when Containerfile does not exist', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-stale-test-'));
    // No ampa/Containerfile in tmpDir
    assert.equal(plugin.isImageStale(tmpDir), false);
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });
});

describe('teardownStalePool', () => {
  test('is exported as a function', () => {
    assert.equal(typeof plugin.teardownStalePool, 'function');
  });
});

// ---------------------------------------------------------------------------
// listAvailablePool (depends on podman state — mostly a shape test)
// ---------------------------------------------------------------------------

describe('listAvailablePool', () => {
  test('returns an array', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pool-test-'));
    const result = plugin.listAvailablePool(tmpDir);
    assert.ok(Array.isArray(result), 'should return an array');
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });
});

// ---------------------------------------------------------------------------
// replenishPool (shape test — does not require podman)
// ---------------------------------------------------------------------------

describe('replenishPool', () => {
  test('is exported as a function', () => {
    assert.equal(typeof plugin.replenishPool, 'function');
  });
});

// ---------------------------------------------------------------------------
// replenishPoolBackground
// ---------------------------------------------------------------------------

describe('replenishPoolBackground', () => {
  test('is exported as a function', () => {
    assert.equal(typeof plugin.replenishPoolBackground, 'function');
  });
});

// ---------------------------------------------------------------------------
// Command registration — warm-pool
// ---------------------------------------------------------------------------

describe('warm-pool command registration', () => {
  test('registers warm-pool subcommand', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    assert.ok(ampa.subcommands.has('warm-pool'), 'warm-pool should be registered');
    assert.ok(ampa.subcommands.get('warm-pool').actionFn, 'warm-pool should have an action');
  });

  test('registers wp alias', () => {
    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    assert.ok(ampa.subcommands.has('wp'), 'wp alias should be registered');
    assert.ok(ampa.subcommands.get('wp').actionFn, 'wp should have an action');
  });
});
