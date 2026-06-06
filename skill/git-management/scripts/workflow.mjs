#!/usr/bin/env node
/**
 * workflow.mjs — Full lifecycle workflow orchestrator.
 *
 * Usage:
 *   node skill/git-management/scripts/workflow.mjs <work-item-id> <short-desc> [--dry-run] [--phase <name>] [--json]
 *
 * Orchestrates: branch → commit → push → PR → merge → cleanup.
 * Composes the standalone step scripts rather than duplicating logic.
 * Supports stopping/resuming at specific phases.
 *
 * Phases (in order):
 *   1. branch    — Create and check out a feature branch
 *   2. commit    — Stage and commit changes
 *   3. push      — Push to remote
 *   4. create-pr — Create a GitHub PR
 *   5. merge-pr  — Merge the PR (requires PR number from previous step)
 *   6. cleanup   — Post-merge branch cleanup
 *
 * Exit codes:
 *   0 — Success
 *   1 — General error
 *   2 — Safety violation
 *   3 — Prerequisite not met
 */

import { execSync } from 'node:child_process';
import {
  parseArgs,
  hasFlag,
  getFlag,
  jsonOutput,
  humanMsg,
  humanError,
  humanSuccess,
  checkPrerequisites,
  safeExec,
  validateWorkItemId,
  makeSlug,
  EXIT,
} from './git-mgmt-helpers.mjs';

import {
  makeBranchName,
  validateBranchName,
  isBranchBlocked,
} from '../../ship/scripts/git-helpers.js';

import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const SCRIPTS_DIR = join(dirname(__filename));

// ── Phase definitions ────────────────────────────────────────────────────────

const PHASES = [
  { name: 'branch', description: 'Create and check out a feature branch' },
  { name: 'commit', description: 'Stage and commit changes' },
  { name: 'push', description: 'Push to remote' },
  { name: 'create-pr', description: 'Create a GitHub PR' },
  { name: 'merge-pr', description: 'Merge the PR' },
  { name: 'cleanup', description: 'Post-merge branch cleanup' },
];

/**
 * Run a phase script and return structured result.
 * @param {string} phaseName
 * @param {string[]} args
 * @returns {{ success: boolean, stdout: string, stderr: string, exitCode: number }}
 */
function runPhase(phaseName, args) {
  const scriptPath = join(SCRIPTS_DIR, `${phaseName}.mjs`);
  const cmd = `node "${scriptPath}" ${args.join(' ')}`;
  return safeExec(cmd);
}

// ── Main ─────────────────────────────────────────────────────────────────────

function main() {
  const { flags, positional } = parseArgs(process.argv.slice(2));
  const dryRun = hasFlag(flags, 'dry-run');
  const asJson = hasFlag(flags, 'json');
  const resumePhase = getFlag(flags, 'phase');

  // Require work-item ID
  if (positional.length < 1) {
    const msg = 'Usage: workflow <work-item-id> [<short-desc>] [--dry-run] [--phase <name>] [--json]';
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
    humanError(msg, EXIT.GENERAL_ERROR);
  }

  const workItemId = positional[0];
  const shortDesc = positional.length > 1 ? positional.slice(1).join('-') : 'feature-work';

  // Validate work-item ID
  const idValidation = validateWorkItemId(workItemId);
  if (!idValidation.valid) {
    if (asJson) jsonOutput({ success: false, error: idValidation.reason }, EXIT.GENERAL_ERROR);
    humanError(idValidation.reason, EXIT.GENERAL_ERROR);
  }

  // Generate branch name
  const slug = makeSlug(shortDesc);
  const branchName = makeBranchName(workItemId, slug);

  // Determine starting phase
  let startIdx = 0;
  if (resumePhase) {
    startIdx = PHASES.findIndex(p => p.name === resumePhase);
    if (startIdx === -1) {
      const msg = `Unknown phase "${resumePhase}". Valid phases: ${PHASES.map(p => p.name).join(', ')}`;
      if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
      humanError(msg, EXIT.GENERAL_ERROR);
    }
  }

  // Dry-run: show the plan
  if (dryRun) {
    const plan = PHASES.slice(startIdx).map(p => ({
      phase: p.name,
      description: p.description,
    }));

    const result = {
      success: true,
      dryRun: true,
      workItemId,
      branchName,
      phasesToRun: plan,
      message: `Dry-run plan for ${workItemId}: ${plan.length} phase(s)`,
    };

    if (asJson) jsonOutput(result);

    humanMsg(`[DRY RUN] Workflow plan for ${workItemId}:`);
    humanMsg(`  Branch: ${branchName}`);
    humanMsg(`  Phases to execute:`);
    for (const p of plan) {
      humanMsg(`    ${p.phase}: ${p.description}`);
    }
    process.exit(EXIT.SUCCESS);
  }

  // Execute phases
  const phaseResults = [];
  let prNumber = '';

  for (let i = startIdx; i < PHASES.length; i++) {
    const phase = PHASES[i];

    humanMsg(`\n── Phase ${i + 1}/${PHASES.length}: ${phase.name} ──`);

    let phaseArgs = ['--json'];

    switch (phase.name) {
      case 'branch':
        phaseArgs.push(workItemId, slug);
        break;
      case 'commit':
        phaseArgs.push('--all', '--work-item', workItemId, '--message', `feat: ${shortDesc}`);
        break;
      case 'push':
        phaseArgs.push('--into-dev');
        break;
      case 'create-pr':
        phaseArgs.push('--base', 'dev');
        break;
      case 'merge-pr':
        if (!prNumber) {
          humanMsg('No PR number from previous step, skipping merge phase');
          phaseResults.push({ phase: phase.name, success: false, skipped: true, reason: 'No PR number' });
          continue;
        }
        phaseArgs.push(prNumber, '--delete-source');
        break;
      case 'cleanup':
        phaseArgs.push('--days', '14');
        break;
    }

    const result = runPhase(phase.name, phaseArgs);

    let parsedResult;
    try {
      parsedResult = JSON.parse(result.stdout);
    } catch {
      parsedResult = { success: false, error: result.stderr || 'Script output was not valid JSON' };
    }

    if (!parsedResult.success) {
      humanMsg(`Phase "${phase.name}" failed: ${parsedResult.error || result.stderr}`);
      phaseResults.push({
        phase: phase.name,
        success: false,
        error: parsedResult.error || result.stderr,
      });

      // Stop on failure
      const finalResult = {
        success: false,
        workItemId,
        branchName,
        failedPhase: phase.name,
        phasesCompleted: phaseResults.filter(r => r.success).map(r => r.phase),
        phaseResults,
        message: `Workflow failed at phase "${phase.name}"`,
      };

      if (asJson) jsonOutput(finalResult, EXIT.GENERAL_ERROR);
      humanError(`Workflow failed at phase "${phase.name}": ${parsedResult.error || result.stderr}`, EXIT.GENERAL_ERROR);
    }

    // Capture PR number from create-pr phase
    if (phase.name === 'create-pr' && parsedResult.prNumber) {
      prNumber = parsedResult.prNumber;
    }

    phaseResults.push({
      phase: phase.name,
      success: true,
      ...parsedResult,
    });
  }

  const result = {
    success: true,
    workItemId,
    branchName,
    phasesCompleted: phaseResults.map(r => r.phase),
    phaseResults,
    message: `Workflow completed for ${workItemId}`,
  };

  if (asJson) jsonOutput(result);
  humanSuccess({
    message: `Workflow completed for ${workItemId}`,
    details: {
      branchName,
      phasesCompleted: phaseResults.map(r => r.phase).join(', '),
    },
  });
}

main();
