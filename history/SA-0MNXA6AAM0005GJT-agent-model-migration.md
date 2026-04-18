Migration changes applied by scripts/migrate_agent_models.py

Files modified:

- agent/Casey.md: model: proxy/gemma4 -> github-copilot/gpt-5.2
  Note: proxy/gemma4 is equivalent to the canonical full agent model mapping; owner should verify behaviour and sign off in the PR.

- agent/patch.md: model: github-copilot/gpt-5.2-codex -> github-copilot/gpt-5.2
  Note: codex suffix removed to use canonical gpt-5.2. Owner should verify that any code-generation behaviour remains acceptable.

Migration script only updates recognized model variants. Files with unknown or custom models remain unchanged and are flagged by the linter for manual review.
