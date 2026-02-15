---
name: Install AMPA Skill
description: |
  Install, upgrade and maintain the ampa Worklog plugin by running the bundled installer script.
---

## Purpose

Provide a simple, canonical installer for AMPA. Installs or upgrades the AMPA plugin for Worklog.

## When to Use

User asks to "Install AMPA", "Install PM Agent", "Upgrade AMPA", "Upgrade PM Agent", "Change AMPA", "Configure AMPA" or similar.

## Usage

1. Establish current status

Run `wl plugins --json` to discover whether the AMPA plugin is currently installed or not.

If AMPA is currently installed AND the skill was activated with either an install or upgrade request display a message indicated that the installation will be upgraded using
the existing configuration and instructing the requestor to request to "Configure AMPA" if they wish to change the configuraiton.

If there is currently no installation or the skill was activated with a request to configure or change AMPA continue to step 2, otherwise skip to step 3.

2. Discord Webhook

If a webhook was provided in the prompt that triggered this skill skip ahead to the next step.

Explain that a discord webhook is required for notifications from the AMPA agent and request the URL for the webhook.

3. Install/Upgrade AMPA

Run the installer from the repository root providing any configuration options we have been given. If no options have been given then run the installer with only the --yes flag.

For example:

```
skill/install-ampa/scripts/install-worklog-plugin.sh --webhook <discord_webhook> --yes
```

Notes:
- The script writes logs and decision traces under `/tmp` (e.g. `/tmp/ampa_install_decisions.<pid>` and `/tmp/ampa_install_*.log`).
