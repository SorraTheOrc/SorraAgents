#!/usr/bin/env node
// run-release.js — safe wrapper to invoke repository-level release script
// Usage: node run-release.js [--dry-run] [--work-item-id <id>] [--force] [--skip-checks]

import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';
import { checkUnmergedBranches } from './check-unmerged-branches.js';

// Canonical release script path relative to repository root
const REPO_RELEASE_SCRIPT = 'scripts/release/merge-dev-to-main.sh';

// Also accept a skill-level release script (e.g., installed under the skill directory)
// Skill layout: <skill-dir>/scripts/release/merge-dev-to-main.sh
const skillDir = dirname(dirname(fileURLToPath(import.meta.url)));
const SKILL_RELEASE_SCRIPT = join(skillDir, 'scripts', 'release', 'merge-dev-to-main.sh');

// ── Parse CLI arguments ──────────────────────────────────────────────────────

const args = process.argv.slice(2);
const skipChecks = args.includes('--skip-checks');

// ── Step 1: Check for unmerged branches (gating step) ────────────────────────

if (!skipChecks) {
  const report = checkUnmergedBranches();
  if (report.hasUnmergedBranches) {
    console.error(
      '⚠️  Gating check failed — there are unmerged local branches that should be resolved first:\n',
    );
    console.error(report.message);
    console.error(
      '\nTo bypass this check, re-run with --skip-checks.',
    );
    process.exitCode = 3;
    process.exit(3);
  }
}

// ── Step 2: Find the release script ─────────────────────────────────────────

let selectedScript = null;
if (existsSync(SKILL_RELEASE_SCRIPT)) {
  selectedScript = SKILL_RELEASE_SCRIPT;
} else if (existsSync(REPO_RELEASE_SCRIPT)) {
  selectedScript = REPO_RELEASE_SCRIPT;
}

if (!selectedScript) {
  const msg = [
    `Ship automated release unavailable: missing canonical release script.`,
    '',
    'Attempted locations: ',
    ` - skill: ${SKILL_RELEASE_SCRIPT}`,
    ` - repository: ${resolve(REPO_RELEASE_SCRIPT)}`,
    '',
    'Human fallback: perform the dev → main promotion manually using the Release Manager checklist:',
    '- See docs/dev/release-process.md for the manual merge workflow and checklist.',
    '- Example manual commands (from repo root):',
    '    git fetch origin',
    '    git checkout main',
    '    git merge origin/dev --no-ff',
    '    git push origin main',
    '',
    "If you want the agent to run an automated release, place the canonical script at '<skill-dir>/scripts/release/merge-dev-to-main.sh' or add it to the repository at 'scripts/release/merge-dev-to-main.sh'.",
  ].join('\n');

  // Write to stderr so that callers (agents) can detect failure and present it to the operator
  console.error(msg);
  process.exitCode = 2;
  process.exit(2);
}

// ── Step 3: Execute the release script ──────────────────────────────────────

const child = spawnSync('bash', [selectedScript, ...args], { stdio: 'inherit' });
process.exitCode = child.status || 0;
process.exit(process.exitCode);
