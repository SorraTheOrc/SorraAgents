/**
 * Test helpers for interacting with the Worklog (wl) CLI.
 *
 * Provides utilities for creating, querying, and cleaning up work items
 * during integration tests.
 */

import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const exec = promisify(execFile);

/**
 * Run a `wl` command and return parsed JSON output.
 *
 * @param {string[]} args - Arguments to pass to the `wl` command.
 * @returns {Promise<object>} Parsed JSON output from wl.
 */
export async function wl(...args) {
  const { stdout } = await exec('wl', [...args, '--json'], {
    encoding: 'utf-8',
  });
  try {
    return JSON.parse(stdout);
  } catch {
    throw new Error(`Failed to parse wl output for: wl ${args.join(' ')}\n${stdout}`);
  }
}

/**
 * Create a work item via `wl create` and return the created item.
 *
 * @param {object} opts
 * @param {string} opts.title - Work item title.
 * @param {string} opts.description - Markdown description.
 * @param {string} [opts.priority] - Priority level.
 * @param {string} [opts.parentId] - Parent work item ID.
 * @param {string} [opts.tags] - Comma-separated tags.
 * @returns {Promise<object>} The created work item.
 */
export async function createWorkItem({ title, description, priority, parentId, tags }) {
  const args = ['create', '-t', title, '-d', description];
  if (priority) args.push('--priority', priority);
  if (parentId) args.push('--parent', parentId);
  if (tags) args.push('--tags', tags);
  const result = await wl(...args);
  return result.workItem ?? result;
}

/**
 * Look up a work item by ID.
 *
 * @param {string} id - Work item ID.
 * @returns {Promise<object>} The work item details.
 */
export async function getWorkItem(id) {
  const result = await wl('show', id);
  return result.workItem ?? result;
}

/**
 * Search for work items matching a query.
 *
 * @param {string} query - Search terms.
 * @returns {Promise<object[]>} Matching work items.
 */
export async function searchWorkItems(query) {
  const result = await wl('search', query);
  return result.results ?? result.items ?? result.workItems ?? result;
}

/**
 * List work items filtered by tag.
 *
 * @param {string} tag - Tag to filter by.
 * @returns {Promise<object[]>} Matching work items.
 */
export async function listByTag(tag) {
  const result = await wl('list', '--tags', tag);
  return result.items ?? result.workItems ?? result;
}

/**
 * Generate a unique tag for test isolation.
 *
 * @param {string} prefix - Prefix for the tag.
 * @returns {string} Unique tag string.
 */
export function uniqueTestTag(prefix = 'test') {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}
