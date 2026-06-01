#!/usr/bin/env node
// run-release.js — safe wrapper to invoke repository-level release script
// Usage: node run-release.js [--dry-run] [--work-item-id <id>] [--force]

import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

// The canonical release script path relative to repository root
const RELEASE_SCRIPT = 'scripts/release/merge-dev-to-main.sh';

// Defensive check: ensure the release script exists in the current repo
if (!existsSync(RELEASE_SCRIPT)) {
  const msg = [
    `Ship automated release unavailable: repository is missing the canonical release script '${RELEASE_SCRIPT}'.`,
    '',
    'Human fallback: perform the dev → main promotion manually using the Release Manager checklist:',
    '- See docs/dev/release-process.md for the manual merge workflow and checklist.',
    '- Example manual commands (from repo root):',
    '    git fetch origin',
    '    git checkout main',
    '    git merge origin/dev --no-ff',
    '    git push origin main',
    '',
    "If you want the agent to run an automated release, add the canonical script at 'scripts/release/merge-dev-to-main.sh' in the repository, or use the Ship subagent configured for this repo.",
  ].join('\n');

  // Write to stderr so that callers (agents) can detect failure and present it to the operator
  console.error(msg);
  process.exitCode = 2;
  process.exit(2);
}

// If the script exists, forward arguments and execute it
const args = process.argv.slice(2);
const child = spawnSync('bash', [RELEASE_SCRIPT, ...args], { stdio: 'inherit' });
process.exitCode = child.status || 0;
process.exit(process.exitCode);
