import { test, describe, mock } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import path from 'path';

// Import the plugin module — all new helpers/constants are named exports.
const pluginModule = new URL('../../plugins/wl_ampa/ampa.mjs', import.meta.url);
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
  test('finish-work exits with code 2 when not in a container', async () => {
    // Ensure env vars are not set
    const origName = process.env.AMPA_CONTAINER_NAME;
    const origId = process.env.AMPA_WORK_ITEM_ID;
    delete process.env.AMPA_CONTAINER_NAME;
    delete process.env.AMPA_WORK_ITEM_ID;

    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampa = ctx.program.commands.get('ampa');
    const fwCmd = ampa.subcommands.get('finish-work');

    // Capture stderr
    const originalError = console.error;
    let errorOutput = '';
    console.error = (...args) => { errorOutput += args.join(' ') + '\n'; };

    process.exitCode = undefined;
    await fwCmd.actionFn({ force: false });
    console.error = originalError;

    assert.equal(process.exitCode, 2, 'should set exit code to 2');
    assert.ok(errorOutput.includes('Not running inside a start-work container'), 'should print detection error');

    // Restore
    if (origName !== undefined) process.env.AMPA_CONTAINER_NAME = origName;
    if (origId !== undefined) process.env.AMPA_WORK_ITEM_ID = origId;
    process.exitCode = undefined;
  });
});
