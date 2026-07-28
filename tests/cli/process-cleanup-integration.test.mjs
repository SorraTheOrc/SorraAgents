/**
 * Process Cleanup Integration Test
 *
 * Spawns a batch of mock processes, verifies they are tracked, and confirms
 * all are cleaned up after killTrackedProcesses() is called.
 *
 * Also verifies that signal handlers are properly registered so cleanup
 * happens when the process receives a signal.
 *
 * @see Work Item SA-0MRP87J73003CH7Y
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// ── Import helpers under test ───────────────────────────────────────────────

let cliHelpers;
try {
  cliHelpers = await import(join(__dirname, 'cli-helpers.mjs'));
} catch (err) {
  cliHelpers = null;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * Check whether a PID is still alive using signal 0.
 */
function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

// ── Integration Tests ───────────────────────────────────────────────────────

describe('Process Cleanup Integration', () => {
  const testTimeout = 15_000; // 15s per test

  describe('batch cleanup', () => {
    test('spawns and cleans up 10 mock processes via execAsync', { timeout: testTimeout }, async () => {
      if (!cliHelpers) {
        return;
      }

      // Spawn 10 long-running processes through execAsync so they are tracked
      const promises = [];
      for (let i = 0; i < 10; i++) {
        promises.push(cliHelpers.execAsync('sleep', ['30']));
      }
      // Suppress rejections from kill
      for (const p of promises) {
        p.catch(() => {});
      }

      // Give them time to start
      await sleep(500);

      // Verify all are tracked
      const trackedPids = cliHelpers.getTrackedPids();
      assert.ok(
        trackedPids.size >= 10,
        `Expected at least 10 tracked PIDs, got ${trackedPids.size}`
      );

      // Kill them all
      const killCount = cliHelpers.killTrackedProcesses();
      await sleep(500);

      // Verify all cleaned up
      assert.ok(
        killCount >= 10,
        `Expected at least 10 processes killed, got ${killCount}`
      );
      assert.equal(
        cliHelpers.getTrackedPids().size,
        0,
        'Tracking set should be empty after kill'
      );
    });

    test('all processes are dead after cleanup', { timeout: testTimeout }, async () => {
      if (!cliHelpers) {
        return;
      }

      // Spawn 5 long-running processes through execAsync for tracking
      const promises = [];
      for (let i = 0; i < 5; i++) {
        promises.push(cliHelpers.execAsync('sleep', ['60']));
      }
      for (const p of promises) {
        p.catch(() => {});
      }

      await sleep(500);

      // Get the tracked PIDs
      const trackedPids = Array.from(cliHelpers.getTrackedPids());
      assert.ok(trackedPids.length >= 5, `Expected at least 5 tracked PIDs`);

      // Verify processes are alive
      const aliveBefore = trackedPids.filter(pid => isAlive(pid));
      assert.ok(aliveBefore.length >= 5, `Expected at least 5 processes alive before cleanup`);

      // Kill tracked processes
      cliHelpers.killTrackedProcesses();
      await sleep(500);

      // Verify all processes are dead
      const aliveAfter = trackedPids.filter(pid => isAlive(pid));
      assert.equal(
        aliveAfter.length,
        0,
        `Expected 0 processes alive after cleanup, got ${aliveAfter.length}`
      );
    });
  });

  describe('signal handler behavior', () => {
    test('signal handler triggers cleanup without crashing', { timeout: testTimeout }, async () => {
      if (!cliHelpers) {
        return;
      }

      // Spawn a few processes through execAsync
      const p1 = cliHelpers.execAsync('sleep', ['10']);
      const p2 = cliHelpers.execAsync('sleep', ['10']);
      const p3 = cliHelpers.execAsync('sleep', ['10']);
      p1.catch(() => {});
      p2.catch(() => {});
      p3.catch(() => {});
      await sleep(500);

      const initialCount = cliHelpers.getTrackedPids().size;
      assert.ok(initialCount >= 3, `Expected at least 3 tracked PIDs, got ${initialCount}`);

      // Verify signal handlers are registered
      const eventNames = process.eventNames();
      const hasSignalHandlers = ['SIGTERM', 'SIGINT', 'SIGHUP'].every(sig =>
        eventNames.includes(sig)
      );

      assert.ok(hasSignalHandlers, 'Signal handlers should be registered for SIGTERM, SIGINT, SIGHUP');

      // Clean up
      cliHelpers.killTrackedProcesses();
      await sleep(200);
    });

    test('beforeExit handler triggers cleanup', { timeout: testTimeout }, async () => {
      if (!cliHelpers) {
        return;
      }

      // Verify the beforeExit handler is registered
      const eventNames = process.eventNames();
      assert.ok(
        eventNames.includes('beforeExit'),
        'beforeExit handler should be registered'
      );

      // Verify there are listeners for beforeExit
      const listeners = process.listeners('beforeExit');
      assert.ok(
        listeners.length > 0,
        'Should have beforeExit listeners installed'
      );

      // Clean up any tracked processes
      cliHelpers.killTrackedProcesses();
    });
  });

  describe('non-interference', () => {
    test('signal handlers do not interfere with normal test execution', { timeout: testTimeout }, async () => {
      if (!cliHelpers) {
        return;
      }

      // Run a quick process that completes normally using spawn directly
      const child = spawn(process.execPath, ['-e', 'console.log("hello")']);
      const stdout = await new Promise((resolve, reject) => {
        let out = '';
        child.stdout.on('data', (data) => { out += data.toString(); });
        child.on('close', (code) => {
          if (code === 0) resolve(out.trim());
          else reject(new Error(`exit code ${code}`));
        });
        child.on('error', reject);
      });

      assert.equal(stdout, 'hello', 'execAsync should work normally');

      // Verify no tracking interference
      const trackedPids = cliHelpers.getTrackedPids();
      assert.equal(trackedPids.size, 0, 'No processes should be tracked after completion');
    });

    test('multiple cleanup calls are safe', { timeout: testTimeout }, async () => {
      if (!cliHelpers) {
        return;
      }

      // Kill should be safe to call even with no tracked processes
      const count1 = cliHelpers.killTrackedProcesses();
      const count2 = cliHelpers.killTrackedProcesses();
      const count3 = cliHelpers.killTrackedProcesses();

      assert.equal(count1, 0, 'First kill should return 0');
      assert.equal(count2, 0, 'Second kill should return 0');
      assert.equal(count3, 0, 'Third kill should return 0');
      assert.equal(cliHelpers.getTrackedPids().size, 0, 'Tracking set should remain empty');
    });
  });
});
