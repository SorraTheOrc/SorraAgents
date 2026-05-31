/**
 * Dev-branch critical tests — deeper checks that verify the critical path
 * of the project works correctly.  These tests are slightly slower than
 * smoke tests but still run in under 10 minutes.
 *
 * Run locally from the repository root:
 *
 *   node --test tests/dev/critical.mjs
 *
 * Designed to catch problems that smoke tests might miss:
 *
 *  1. Full pytest suite collects and at least a subset passes
 *  2. All skill SKILL.md files are present and parseable
 *  3. Worklog data file integrity
 *  4. Agent guidance files (AGENTS.md, Workflow.md) are consistent
 *  5. CI workflow YAML files are valid
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execSync } from 'node:child_process';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = join(fileURLToPath(import.meta.url), '..', '..', '..');

/** Helper: run a command from the repo root; returns { stdout, stderr, exitCode }. */
function run(cmd, opts = {}) {
  try {
    const stdout = execSync(cmd, {
      cwd: REPO_ROOT,
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
      ...opts,
    });
    return { stdout, stderr: '', exitCode: 0 };
  } catch (err) {
    return {
      stdout: err.stdout ?? '',
      stderr: err.stderr ?? '',
      exitCode: err.status ?? 1,
    };
  }
}

// ---------------------------------------------------------------------------
// 1. Full pytest suite — at least collects without errors
// ---------------------------------------------------------------------------
test('critical: full pytest suite collects successfully', () => {
  const result = run('python3 -m pytest --collect-only -q 2>&1');
  assert.ok(
    result.exitCode <= 1,
    `pytest collection failed with exit code ${result.exitCode}: ${result.stderr}`,
  );
  assert.ok(
    result.stdout.includes('collected') || result.stdout.includes('test_'),
    'pytest should report collected test items',
  );
});

// ---------------------------------------------------------------------------
// 2. A subset of pytest tests actually pass
// ---------------------------------------------------------------------------
test('critical: a subset of python tests pass', () => {
  // Run a quick subset to verify the test infrastructure works
  const result = run('python3 -m pytest tests/test_detection.py tests/test_terminology_check.py -v --tb=short 2>&1', { timeout: 60000 });
  // Exit 0 = all pass, 1 = some fail (we still want to see they run)
  assert.ok(
    result.exitCode <= 1,
    `pytest subset failed with exit code ${result.exitCode}: ${result.stderr}`,
  );
  assert.ok(
    result.stdout.includes('passed') || result.stdout.includes('PASSED'),
    'At least some tests should pass',
  );
});

// ---------------------------------------------------------------------------
// 3. All skill SKILL.md files are present and have required frontmatter
// ---------------------------------------------------------------------------
test('critical: all skills have valid SKILL.md files', () => {
  const skillDir = join(REPO_ROOT, 'skill');
  assert.ok(existsSync(skillDir), 'skill/ directory should exist');

  const skills = readdirSync(skillDir).filter((name) => {
    if (name === '__pycache__' || name.startsWith('.')) return false;
    const full = join(skillDir, name);
    if (!statSync(full).isDirectory()) return false;
    // Exclude Python module directories (those with __init__.py but no SKILL.md)
    if (existsSync(join(full, '__init__.py')) && !existsSync(join(full, 'SKILL.md')))
      return false;
    return true;
  });

  assert.ok(skills.length > 0, 'At least one skill directory should exist');

  for (const skill of skills) {
    const skillMd = join(skillDir, skill, 'SKILL.md');
    assert.ok(existsSync(skillMd), `Skill "${skill}" should have a SKILL.md file`);

    const content = readFileSync(skillMd, 'utf-8');
    // Check for YAML frontmatter
    assert.ok(
      content.startsWith('---'),
      `Skill "${skill}" SKILL.md should start with YAML frontmatter`,
    );
    // Check that it has a name field in frontmatter
    assert.ok(
      content.includes('name:'),
      `Skill "${skill}" SKILL.md frontmatter should include a name field`,
    );
  }
});

// ---------------------------------------------------------------------------
// 4. CI workflow YAML files are valid
// ---------------------------------------------------------------------------
test('critical: CI workflow YAML files are valid', () => {
  const yamlCheck = run('python3 -c "import yaml" 2>&1');
  if (yamlCheck.exitCode !== 0) {
    // pyyaml not installed — install it temporarily
    run('pip install pyyaml 2>&1');
  }

  const workflowsDir = join(REPO_ROOT, '.github', 'workflows');
  assert.ok(existsSync(workflowsDir), '.github/workflows/ directory should exist');

  const workflowFiles = readdirSync(workflowsDir).filter((name) =>
    name.endsWith('.yml') || name.endsWith('.yaml'),
  );

  assert.ok(workflowFiles.length > 0, 'At least one workflow file should exist');

  for (const wf of workflowFiles) {
    const wfPath = join(workflowsDir, wf);
    const result = run(`python3 -c "import yaml, sys; yaml.safe_load(open('${wfPath}'))" 2>&1`);
    assert.equal(
      result.exitCode,
      0,
      `Workflow file "${wf}" should be valid YAML: ${result.stderr}`,
    );
  }
});

// ---------------------------------------------------------------------------
// 5. Agent guidance files consistency
// ---------------------------------------------------------------------------
test('critical: AGENTS.md and Workflow.md reference consistent terminology', () => {
  const agentsMd = readFileSync(join(REPO_ROOT, 'AGENTS.md'), 'utf-8');
  const workflowMd = readFileSync(join(REPO_ROOT, 'Workflow.md'), 'utf-8');

  // Both files should reference worklog/wl consistently
  const hasWlReference = (content) =>
    content.includes('wl ') || content.includes('Worklog') || content.includes('work-item');

  assert.ok(hasWlReference(agentsMd), 'AGENTS.md should reference worklog/wl');
  assert.ok(hasWlReference(workflowMd), 'Workflow.md should reference worklog/wl');

  // Both should mention the core workflow stages
  for (const stage of ['in_progress', 'in_review']) {
    assert.ok(
      agentsMd.includes(stage),
      `AGENTS.md should reference stage "${stage}"`,
    );
  }
});

// ---------------------------------------------------------------------------
// 6. AGENTS.md workflow: agents push to dev, ship handles release to main
// ---------------------------------------------------------------------------
test('critical: AGENTS.md workflow pushes to dev; ship handles release to main', () => {
  const agentsMd = readFileSync(join(REPO_ROOT, 'AGENTS.md'), 'utf-8');

  // Step 5 should describe pushing into dev as the integration step
  assert.ok(
    agentsMd.includes('push into dev') || agentsMd.includes('push.*dev') ||
    (agentsMd.includes('git push') && agentsMd.includes('refs/heads/dev')),
    'AGENTS.md step 5 should describe pushing into dev',
  );

  // Step 6 should state that regular agents do NOT merge to main
  assert.ok(
    agentsMd.includes('do NOT merge') || agentsMd.includes('not merge to main') ||
    agentsMd.includes('agents does not merge'),
    'AGENTS.md should state regular agents do not merge to main',
  );

  // Should reference the pre-push hook for enforcement
  assert.ok(
    agentsMd.includes('pre-push hook') || agentsMd.includes('.githooks/pre-push'),
    'AGENTS.md should reference the pre-push hook',
  );

  // Should reference the ship skill for the dev→main release process
  assert.ok(
    agentsMd.includes('ship agent') || agentsMd.includes('ship/skill') ||
    agentsMd.includes('skill/ship/SKILL.md'),
    'AGENTS.md should reference the ship skill',
  );

  // Should reference the release merge script for dev→main
  assert.ok(
    agentsMd.includes('merge-dev-to-main.sh') || agentsMd.includes('scripts/release'),
    'AGENTS.md should reference the release merge script',
  );
});

// ---------------------------------------------------------------------------
// 7. skill/implement/SKILL.md step 5 describes dev-push workflow
// ---------------------------------------------------------------------------
test('critical: implement skill step 5 describes dev-push workflow, not PR creation', () => {
  const skillMd = readFileSync(join(REPO_ROOT, 'skill/implement/SKILL.md'), 'utf-8');

  // The Outputs section should mention dev push and in_review, not PR URL
  assert.ok(
    !skillMd.includes('Pull Request URL'),
    'Outputs section should NOT reference Pull Request URL creation',
  );
  assert.ok(
    skillMd.includes('pushed to dev') || skillMd.includes('push to dev'),
    'Outputs section should mention pushing to dev',
  );

  // Step 5 should describe dev push
  assert.ok(
    skillMd.includes('Push to dev') || skillMd.includes('push to dev') || skillMd.includes('pushToDev'),
    'Step 5 should describe pushing to dev',
  );

  // Step 5 should NOT instruct agents to create a PR (it may say "do NOT create" but
  // must NOT contain an affirmative instruction to create a PR)
  const step5Section = skillMd.split(/5\.\s*Commit, Push/)?.[1] || '';
  const affirmativePR = /(?<!NOT\s)create a Pull Request\b/i;
  // Also check that the old affirmative PR creation pattern is gone
  assert.ok(
    !step5Section.includes('Create a PR') &&
    !step5Section.includes('Push the branch to `origin`') &&
    !step5Section.includes('PR against the repository'),
    'Step 5 should NOT contain affirmative PR creation instructions',
  );
  // The do-NOT-create mention IS expected — verify it says "Do NOT create"
  assert.ok(
    step5Section.includes('NOT create a Pull Request') ||
    step5Section.includes('NOT create a PR'),
    'Step 5 should warn agents NOT to create a PR to main',
  );

  // Step 5 should warn that work-items stay open
  assert.ok(
    skillMd.includes('NOT close') || skillMd.includes('not close') || skillMd.includes('stays open') || skillMd.includes('stay open'),
    'Step 5 should warn that work-items are NOT closed at this stage',
  );

  // Should reference the ship skill for push-to-dev mechanism
  assert.ok(
    skillMd.includes('ship skill') || skillMd.includes('pushToDev'),
    'Step 5 should reference the ship skill or pushToDev()',
  );
});

// ---------------------------------------------------------------------------
// 8. docs/ralph.md merge safety documents branch protection interaction
// ---------------------------------------------------------------------------
test('critical: ralph.md merge safety documents branch protection interaction', () => {
  const ralphMd = readFileSync(join(REPO_ROOT, 'docs/ralph.md'), 'utf-8');

  // Should document that --confirm-merge uses direct push which may fail
  // with branch protection on main
  assert.ok(
    ralphMd.includes('branch protection') || ralphMd.includes('direct push'),
    'docs/ralph.md should mention branch protection or direct-push limitation',
  );

  // Should document a PR-based merge alternative for protected branches
  assert.ok(
    ralphMd.includes('PR-based') || ralphMd.includes('pull request') || ralphMd.includes('gh pr'),
    'docs/ralph.md should document a PR-based merge alternative',
  );

  // Should reference the release merge script as recommended approach
  // for protected branches
  assert.ok(
    ralphMd.includes('merge-dev-to-main.sh') || ralphMd.includes('scripts/release'),
    'docs/ralph.md should reference the release merge script',
  );
});

// ---------------------------------------------------------------------------
// 8. Worklog data integrity (basic)
// ---------------------------------------------------------------------------
test('critical: wl CLI is functional and returns data', () => {
  // Verify the worklog system is operational by running a simple query
  const result = run('wl list -n 1 --json 2>&1');
  assert.equal(
    result.exitCode,
    0,
    `wl CLI should be functional: ${result.stderr}`,
  );

  // Parse the response to verify it returns structured data
  try {
    const data = JSON.parse(result.stdout);
    assert.ok(
      data.success !== undefined,
      'wl list should return a structured JSON response',
    );
  } catch (err) {
    assert.fail(`wl list output should be valid JSON: ${err.message}`);
  }
});

// ---------------------------------------------------------------------------
// 9. Scripts directory integrity
// ---------------------------------------------------------------------------
test('critical: essential scripts are present and executable', () => {
  const scripts = [
    'scripts/check-terminology.sh',
    'scripts/agent_frontmatter_lint.py',
  ];

  for (const script of scripts) {
    const fullPath = join(REPO_ROOT, script);
    assert.ok(existsSync(fullPath), `Script should exist: ${script}`);

    if (script.endsWith('.sh')) {
      const stat = statSync(fullPath);
      // Check if executable bit is set
      assert.ok(
        stat.mode & 0o111,
        `Shell script should be executable: ${script}`,
      );
    }
  }
});
