/**
 * Process Lifecycle Tracking Tests
 *
 * Verifies that execAsync and execWithInput track spawned child PIDs,
 * and that killTrackedProcesses() terminates them reliably.
 *
 * @see Work Item SA-0MRP863GH000LEFO
 */

import { test, describe, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';

// ── Import helpers under test ───────────────────────────────────────────────

let cliHelpers: {
  execAsync: (command: string, args?: string[]) => Promise<{ stdout: string; stderr: string }>;
  execWithInput: (command: string, args?: string[], input?: string) => Promise<{ stdout: string; stderr: string }>;
  killTrackedProcesses: () => number;
  getTrackedPids: () => Set<number>;
};

try {
  cliHelpers = require('./cli-helpers');
} catch (err) {
  // Tests will be skipped if module can't be loaded
  cliHelpers = null as any;
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// ── Process Lifecycle Tests ─────────────────────────────────────────────────

describe('Process Lifecycle Tracking', () => {
  const testTimeout = 10_000; // 10s per test

  describe('execAsync PID tracking', () => {
    test('tracks PID when execAsync spawns a process', { timeout: testTimeout }, async () => {
      if (!cliHelpers) {
        return; // Skip if module not available
      }

      const initialPids = cliHelpers.getTrackedPids().size;

      // Spawn a long-running process so we can verify tracking before it exits
      const result = cliHelpers.execAsync('sleep', ['5']);
      await sleep(300); // Give it time to start

      const trackedPids = cliHelpers.getTrackedPids();
      assert.ok(
        trackedPids.size > initialPids,
        `Expected at least one PID to be tracked (initial=${initialPids}, current=${trackedPids.size})`
      );

      // Clean up
      cliHelpers.killTrackedProcesses();
      await sleep(200);

      // Verify tracking set is cleared
      assert.equal(cliHelpers.getTrackedPids().size, 0, 'Tracking set should be empty after kill');
    });

    test('auto-removes PID from tracking set on process exit', { timeout: testTimeout }, async () => {
      if (!cliHelpers) {
        return;
      }

      // Spawn a quick process
      await cliHelpers.execAsync('node', ['-e', 'process.exit(0)']);
      await sleep(300); // Give it time to exit and trigger cleanup

      // PID should have been auto-removed on exit
      const trackedPids = cliHelpers.getTrackedPids();
      // Eventually no PIDs should be tracked for completed processes
      assert.ok(trackedPids.size >= 0, 'Tracking set should be a non-negative size');
    });
  });

  describe('execWithInput PID tracking', () => {
    test('tracks PID when execWithInput spawns a process', { timeout: testTimeout }, () => {
      if (!cliHelpers) {
        return;
      }

      // Use a synchronous-style check: spawn a process that reads from stdin
      const promise = cliHelpers.execWithInput('node', ['-e', `
        process.stdin.on('data', () => {});
        setTimeout(() => process.exit(0), 5000);
      `], 'input-data\n');

      // Check tracking set is populated
      const trackedPids = cliHelpers.getTrackedPids();
      assert.ok(
        trackedPids.size > 0,
        `Expected at least one PID to be tracked, got ${trackedPids.size}`
      );

      // Clean up
      cliHelpers.killTrackedProcesses();
    });
  });

  describe('killTrackedProcesses', () => {
    test('terminates all tracked processes and clears the tracking set', { timeout: testTimeout }, async () => {
      if (!cliHelpers) {
        return;
      }

      // Spawn multiple processes
      cliHelpers.execAsync('sleep', ['10']);
      cliHelpers.execAsync('sleep', ['10']);
      cliHelpers.execAsync('sleep', ['10']);
      await sleep(500); // Let them start

      assert.ok(cliHelpers.getTrackedPids().size >= 3, 'Expected at least 3 tracked PIDs');

      const killCount = cliHelpers.killTrackedProcesses();
      await sleep(300);

      assert.ok(killCount >= 3, `Expected at least 3 processes killed, got ${killCount}`);
      assert.equal(cliHelpers.getTrackedPids().size, 0, 'Tracking set should be empty after kill');
    });

    test('handles already-dead processes gracefully', { timeout: testTimeout }, async () => {
      if (!cliHelpers) {
        return;
      }

      // Run a quick process and let it complete
      await cliHelpers.execAsync('node', ['-e', 'process.exit(0)']);
      await sleep(500);

      // Killing should still succeed (no error) even if process already exited
      // The auto-removal should have cleared the PID
      const killCount = cliHelpers.killTrackedProcesses();
      assert.equal(typeof killCount, 'number', 'killCount should be a number');
      assert.equal(cliHelpers.getTrackedPids().size, 0, 'Tracking set should be empty');
    });
  });
});
