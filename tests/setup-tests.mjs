/**
 * Global Test Setup — Graceful Shutdown
 *
 * Registers signal handlers (SIGTERM, SIGINT, SIGHUP, beforeExit) that
 * invoke killTrackedProcesses() so orphaned mock processes are cleaned up
 * when the test process exits (normally or by signal).
 *
 * Import this module in your test runner or setup file to enable automatic
 * cleanup. The handlers are idempotent — importing multiple times is safe.
 *
 * @see Work Item SA-0MRP87J73003CH7Y
 */

import { killTrackedProcesses } from './cli/cli-helpers.mjs';

// Install signal handlers at module scope
const handler = () => {
  killTrackedProcesses();
};

process.on('SIGTERM', handler);
process.on('SIGINT', handler);
process.on('SIGHUP', handler);
process.on('beforeExit', handler);
