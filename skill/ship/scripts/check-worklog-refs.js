/**
 * check-worklog-refs.js — Worklog-ref gating step for the ship skill.
 *
 * Detects the presence of worklog refs (under refs/worklog/) and returns
 * a structured report. This is used as a gating step before any operation
 * that could accidentally merge worklog data into main or dev.
 *
 * The worklog ref (refs/worklog/data) is an orphan ref with no common
 * ancestor with main or dev. Merging it would replace .worklog/worklog-data.jsonl
 * with a stale snapshot, corrupting the worklog database for all users.
 *
 * Usage:
 *
 *   import { checkWorklogRefs } from './check-worklog-refs.js';
 *
 *   const report = checkWorklogRefs();
 *   if (report.hasWorklogRefs) {
 *     console.error(report.message);
 *     // Block the operation
 *   }
 */

import { execSync } from 'node:child_process';

// ── checkWorklogRefs ─────────────────────────────────────────────────────────

/**
 * Check for the presence of worklog refs under refs/worklog/.
 *
 * Uses `git for-each-ref refs/worklog/` to detect any refs in the worklog
 * namespace. Remote-tracking mirror refs (refs/worklog/remotes/**) are
 * excluded — they are harmless bookkeeping maintained by `wl sync` and
 * are never merged by `git merge`/`git pull`.
 *
 * The worklog's own git-branch sync mechanism (refs/worklog/data) is also
 * excluded when it actually contains the worklog data file
 * (.worklog/worklog-data.jsonl): it is worklog DATA, never a code branch,
 * and is never merged into main/dev. Only genuinely dangerous orphan
 * worklog refs (no data file) are flagged.
 *
 * @returns {{
 *   hasWorklogRefs: boolean,
 *   refs: string[],
 *   message: string
 * }}
 */
export function checkWorklogRefs() {
  try {
    const output = execSync('git for-each-ref refs/worklog/ --format="%(refname)"', {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    const allRefs = output
      .split('\n')
      .map((line) => line.trim())
      .filter((line) => line.length > 0);

    // Filter out remote-tracking mirror refs (refs/worklog/remotes/**) and
    // the legitimate worklog data branch (refs/worklog/data) when it holds
    // the worklog data file. These are harmless bookkeeping maintained by
    // `wl sync` and are never merged by `git merge`/`git pull` of a branch.
    const refs = allRefs.filter((ref) => {
      if (ref.startsWith('refs/worklog/remotes/')) return false;
      if (ref === 'refs/worklog/data') {
        try {
          execSync('git cat-file -e refs/worklog/data:.worklog/worklog-data.jsonl', {
            encoding: 'utf-8',
            stdio: ['pipe', 'pipe', 'pipe'],
          });
          return false; // contains worklog data → legitimate sync branch
        } catch {
          // fall through: flagged as a dangerous orphan
        }
      }
      return true;
    });

    if (refs.length === 0) {
      return {
        hasWorklogRefs: false,
        refs: [],
        message: 'No worklog refs detected.',
      };
    }

    return {
      hasWorklogRefs: true,
      refs,
      message: [
        `Found ${refs.length} worklog ref(s) that must not be merged into main or dev:`,
        '',
        ...refs.map((ref) => `  - ${ref}`),
        '',
        'Worklog refs are orphan refs with no common ancestor with main or dev.',
        'Merging them would corrupt the worklog database for all users.',
      ].join('\n'),
    };
  } catch (err) {
    // If git for-each-ref fails (e.g., not a git repo, or refs namespace doesn't exist),
    // treat it as "no worklog refs detected" rather than blocking the operation.
    return {
      hasWorklogRefs: false,
      refs: [],
      message: 'Unable to check for worklog refs (git command failed). Proceeding without gating.',
    };
  }
}
