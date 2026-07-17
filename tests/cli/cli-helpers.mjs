/**
 * CLI Helpers with Process Lifecycle Tracking
 *
 * Provides execAsync and execWithInput wrappers that track spawned child
 * process PIDs for reliable cleanup during testing. Also installs signal
 * handlers so orphaned mock processes are cleaned up when the test process
 * exits normally or receives a signal.
 *
 * @see Work Item SA-0MRP863GH000LEFO
 * @see Work Item SA-0MRP87J73003CH7Y
 */

import { exec as cpExec, spawn } from 'node:child_process';
import { promisify } from 'node:util';

// ── Process Tracking ────────────────────────────────────────────────────────

/**
 * Set of tracked child process PIDs.
 * Populated by execAsync and execWithInput; consumed by killTrackedProcesses().
 */
const pidTrackingSet = new Set();

/**
 * Returns a copy of the current tracked PIDs (for testing/inspection).
 */
export function getTrackedPids() {
  return new Set(pidTrackingSet);
}

/**
 * Register a child process for tracking.
 * Also sets up an exit handler to auto-remove the PID when the child exits.
 */
function trackChild(child) {
  if (child.pid === undefined) {
    return;
  }
  pidTrackingSet.add(child.pid);

  // Auto-remove PID on child exit
  child.on('exit', () => {
    pidTrackingSet.delete(child.pid);
  });
}

// ── execAsync ───────────────────────────────────────────────────────────────

const _exec = promisify(cpExec);

/**
 * Execute a command asynchronously and track the child PID.
 *
 * Wraps child_process.exec so the spawned PID is registered in the tracking
 * set, enabling cleanup via killTrackedProcesses().
 *
 * @param command - The command to execute
 * @param args    - Additional arguments (passed as options to exec)
 * @returns       - Promise resolving with { stdout, stderr }
 */
export async function execAsync(
  command,
  args
) {
  // Use raw exec with callbacks so we can capture the ChildProcess object
  return new Promise((resolve, reject) => {
    const child = cpExec(
      args ? `${command} ${args.join(' ')}` : command,
      (err, stdout, stderr) => {
        if (err) {
          // Include stdout/stderr in the error for diagnostics
          err.stdout = stdout;
          err.stderr = stderr;
          reject(err);
        } else {
          resolve({ stdout, stderr });
        }
      }
    );

    // Track the child PID if available
    if (child.pid !== undefined) {
      pidTrackingSet.add(child.pid);
      child.on('exit', () => {
        pidTrackingSet.delete(child.pid);
      });
    }
  });
}

// ── execWithInput ───────────────────────────────────────────────────────────

/**
 * Execute a command with input piped to its stdin and track the child PID.
 *
 * Uses child_process.spawn to support stdin injection. The spawned PID is
 * registered in the tracking set for cleanup via killTrackedProcesses().
 *
 * @param command - The command to execute
 * @param args    - Command arguments
 * @param input   - Optional string to pipe to stdin
 * @returns       - Promise resolving with { stdout, stderr }
 */
export function execWithInput(
  command,
  args = [],
  input
) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ['pipe', 'pipe', 'pipe'] });

    // Track the child PID
    if (child.pid !== undefined) {
      pidTrackingSet.add(child.pid);
      child.on('exit', () => {
        pidTrackingSet.delete(child.pid);
      });
    }

    let stdout = '';
    let stderr = '';

    child.stdout?.on('data', (data) => {
      stdout += data.toString();
    });

    child.stderr?.on('data', (data) => {
      stderr += data.toString();
    });

    child.on('error', (err) => {
      reject(err);
    });

    child.on('close', (code) => {
      if (code === 0) {
        resolve({ stdout, stderr });
      } else {
        const err = new Error(`Command exited with code ${code}`);
        err.code = code;
        err.stdout = stdout;
        err.stderr = stderr;
        reject(err);
      }
    });

    // Pipe input to stdin if provided
    if (input !== undefined && child.stdin) {
      child.stdin.write(input);
      child.stdin.end();
    }
  });
}

// ── Kill Tracked Processes ──────────────────────────────────────────────────

/**
 * Send SIGTERM to all tracked child processes and clear the tracking set.
 *
 * Uses process.kill with SIGTERM. Falls back to process group kill if the
 * simple kill fails (attempts negative PID for process group).
 *
 * @returns The number of PIDs that were in the tracking set
 */
export function killTrackedProcesses() {
  const pids = Array.from(pidTrackingSet);
  pidTrackingSet.clear();

  for (const pid of pids) {
    try {
      // Try direct SIGTERM first
      process.kill(pid, 'SIGTERM');
    } catch (_err1) {
      try {
        // Fallback: kill process group (negative PID)
        process.kill(-pid, 'SIGTERM');
      } catch (_err2) {
        // Process may already be dead; that's fine
      }
    }
  }

  return pids.length;
}

// ── Signal Handlers ─────────────────────────────────────────────────────────

/**
 * Install signal handlers that invoke killTrackedProcesses() on process exit.
 *
 * Registers handlers for SIGTERM, SIGINT, SIGHUP, and beforeExit so that
 * orphaned mock processes are reliably cleaned up whether the test process
 * exits normally, is killed, or receives a termination signal.
 *
 * Handlers are idempotent — repeated calls are safe.
 */
function installSignalHandlers() {
  // Guard against duplicate installation
  if (installSignalHandlers._installed) {
    return;
  }
  installSignalHandlers._installed = true;

  const handler = () => {
    killTrackedProcesses();
  };

  process.on('SIGTERM', handler);
  process.on('SIGINT', handler);
  process.on('SIGHUP', handler);
  process.on('beforeExit', handler);
}

// Install signal handlers at module scope so any importer gets cleanup
installSignalHandlers();
