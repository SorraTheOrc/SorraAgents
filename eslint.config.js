/** Flat config for ESLint v9+ (SA-0MTG05C3W0066XZZ).
 *
 * Migrated from the legacy .eslintrc.json (which ESLint v9+ no longer
 * loads) so `eslint .` and the code-review linter runner correctly lint
 * only JS/TS files. The critical bug was that the previous setup caused
 * eslint to attempt to parse non-JS files (Python, Markdown) as
 * JavaScript, producing false-positive "Parsing error" findings such as:
 *   - SKILL.md:1 — Assigning to rvalue
 *   - audit_runner.py:2 — Unterminated string constant
 *   - test_audit_runner_freshness.py:1 — Unexpected token __future__
 *
 * The `ignores` entry ensures those file types are never passed to the
 * parser. TypeScript files (.ts/.tsx) are also ignored until a proper
 * parser (@typescript-eslint/parser) is installed — without it they
 * produce unavoidable "Parsing error" diagnostics that would become new
 * false positives.
 */
export default [
  {
    ignores: [
      "**/*.py",
      "**/*.pyi",
      "**/*.md",
      "**/*.markdown",
      "**/*.txt",
      "**/*.json",
      "**/*.yaml",
      "**/*.yml",
      "**/*.sh",
      "**/*.bash",
      "**/*.ini",
      "**/*.cfg",
      "**/*.toml",
      "**/*.ts",
      "**/*.tsx",
      "node_modules/**",
      ".worklog/**",
      ".git/**",
      ".ruff_cache/**",
      "dist/**",
      "coverage/**",
    ],
  },
  {
    files: ["**/*.js", "**/*.mjs", "**/*.cjs", "**/*.jsx"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
    },
    linterOptions: {
      reportUnusedDisableDirectives: false,
    },
    rules: {},
  },
];
