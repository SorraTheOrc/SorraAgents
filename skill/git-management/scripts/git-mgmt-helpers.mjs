/**
 * git-mgmt-helpers.mjs — Shared helpers for git-management scripts.
 *
 * Provides:
 * - Argument parsing with validation
 * - Structured JSON and human-readable output
 * - Prerequisite checking
 * - Safe exec wrapper with structured results
 * - Dry-run support
 */

import { execSync } from 'node:child_process';
import { existsSync } from 'node:fs';

// ── Exit code constants ─────────────────────────────────────────────────────

export const EXIT = Object.freeze({
  SUCCESS: 0,
  GENERAL_ERROR: 1,
  SAFETY_VIOLATION: 2,
  PREREQ_NOT_MET: 3,
});

// ── Output helpers ──────────────────────────────────────────────────────────

/**
 * Print structured JSON output to stdout and exit.
 * @param {object} result - { success, ... }
 * @param {number} exitCode
 */
export function jsonOutput(result, exitCode = EXIT.SUCCESS) {
  console.log(JSON.stringify(result, null, 2));
  process.exit(exitCode);
}

/**
 * Print a human-readable message to stderr.
 * @param {string} msg
 */
export function humanMsg(msg) {
  process.stderr.write(`${msg}\n`);
}

/**
 * Print a human-readable success summary and exit.
 * @param {object} result
 */
export function humanSuccess(result) {
  if (result.message) humanMsg(result.message);
  if (result.details) {
    for (const [key, value] of Object.entries(result.details)) {
      humanMsg(`  ${key}: ${value}`);
    }
  }
  process.exit(EXIT.SUCCESS);
}

/**
 * Print a human-readable error and exit.
 * @param {string} msg
 * @param {number} exitCode
 */
export function humanError(msg, exitCode = EXIT.GENERAL_ERROR) {
  humanMsg(`Error: ${msg}`);
  process.exit(exitCode);
}

// ── Prerequisite checks ─────────────────────────────────────────────────────

/**
 * Check whether a command is available in PATH.
 * @param {string} cmd
 * @returns {boolean}
 */
export function commandExists(cmd) {
  try {
    execSync(`which ${cmd}`, { stdio: ['pipe', 'pipe', 'pipe'] });
    return true;
  } catch {
    return false;
  }
}

/**
 * Check required prerequisites. Returns structured result.
 * @param {string[]} commands - Array of command names (e.g., ['git', 'gh'])
 * @param {object} opts
 * @param {boolean} [opts.requireGitDir=true] - Must be inside a git repository
 * @param {boolean} [opts.requireCleanWorktree=false] - Worktree must be clean
 * @returns {{ ok: boolean, errors: string[] }}
 */
export function checkPrerequisites(commands = ['git'], opts = {}) {
  const { requireGitDir = true, requireCleanWorktree = false } = opts;
  const errors = [];

  for (const cmd of commands) {
    if (!commandExists(cmd)) {
      errors.push(`Required command '${cmd}' is not installed or not in PATH`);
    }
  }

  if (requireGitDir) {
    try {
      execSync('git rev-parse --git-dir', { stdio: ['pipe', 'pipe', 'pipe'] });
    } catch {
      errors.push('Not inside a git repository');
    }
  }

  if (requireCleanWorktree) {
    try {
      const status = execSync('git status --porcelain', {
        encoding: 'utf-8',
        stdio: ['pipe', 'pipe', 'pipe'],
      }).trim();
      if (status !== '') {
        errors.push('Working tree has uncommitted changes');
      }
    } catch {
      errors.push('Unable to check working tree status');
    }
  }

  return { ok: errors.length === 0, errors };
}

// ── Safe exec wrapper ───────────────────────────────────────────────────────

/**
 * Execute a command and return structured result.
 * @param {string} cmd
 * @param {object} [opts]
 * @param {boolean} [opts.json=false] - Parse stdout as JSON
 * @returns {{ success: boolean, stdout: string, stderr: string, exitCode: number }}
 */
export function safeExec(cmd, opts = {}) {
  try {
    const stdout = execSync(cmd, {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
      ...opts,
    });
    return { success: true, stdout: stdout.trim(), stderr: '', exitCode: 0 };
  } catch (err) {
    return {
      success: false,
      stdout: (err.stdout ?? '').toString().trim(),
      stderr: (err.stderr ?? '').toString().trim(),
      exitCode: err.status ?? 1,
    };
  }
}

// ── Argument parsing ────────────────────────────────────────────────────────

/**
 * Parse command-line arguments into a key-value map.
 * Supports: --flag, --flag value, --flag=value, positional args.
 * @param {string[]} args - process.argv.slice(2)
 * @returns {{ flags: Map<string, string|true>, positional: string[] }}
 */
export function parseArgs(args) {
  const flags = new Map();
  const positional = [];
  let i = 0;

  while (i < args.length) {
    const arg = args[i];

    if (arg.startsWith('--')) {
      const eqIdx = arg.indexOf('=');
      if (eqIdx > 2) {
        // --flag=value syntax
        flags.set(arg.slice(2, eqIdx), arg.slice(eqIdx + 1));
        i += 1;
      } else {
        const key = arg.slice(2);
        if (i + 1 < args.length && !args[i + 1].startsWith('--')) {
          flags.set(key, args[i + 1]);
          i += 2;
        } else {
          flags.set(key, true);
          i += 1;
        }
      }
    } else if (arg.startsWith('-') && arg.length === 2) {
      // Short flags like -h
      const key = arg.slice(1);
      if (i + 1 < args.length && !args[i + 1].startsWith('-')) {
        flags.set(key, args[i + 1]);
        i += 2;
      } else {
        flags.set(key, true);
        i += 1;
      }
    } else {
      positional.push(arg);
      i += 1;
    }
  }

  return { flags, positional };
}

/**
 * Check if a flag is set (true or has a value).
 * @param {Map} flags
 * @param {string} key
 * @returns {boolean}
 */
export function hasFlag(flags, key) {
  return flags.has(key);
}

/**
 * Get a flag value, or undefined if not set.
 * @param {Map} flags
 * @param {string} key
 * @returns {string|true|undefined}
 */
export function getFlag(flags, key) {
  return flags.get(key);
}

// ── Work-item ID validation ─────────────────────────────────────────────────

/**
 * Validate a Worklog work-item ID format.
 * Accepts patterns like SA-XXXXXXXXXXXXXXX or WL-XXX.
 * @param {string} id
 * @returns {{ valid: boolean, reason?: string }}
 */
export function validateWorkItemId(id) {
  if (!id || typeof id !== 'string' || id.trim() === '') {
    return { valid: false, reason: 'Work-item ID is required and must be a non-empty string' };
  }

  // Accept SA-<alphanumeric> or any uppercase ID with hyphens
  const pattern = /^[A-Z][A-Z0-9]*-[A-Z0-9]+(-[A-Z0-9]+)*$/;
  if (!pattern.test(id.trim())) {
    return {
      valid: false,
      reason: `Work-item ID "${id}" does not match expected format (e.g., SA-0MPMI7FWI004PXHS)`,
    };
  }

  return { valid: true };
}

// ── Short description slug generation ────────────────────────────────────────

/**
 * Convert a human-readable description to a URL-safe slug.
 * @param {string} desc
 * @returns {string}
 */
export function makeSlug(desc) {
  return desc
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');
}
