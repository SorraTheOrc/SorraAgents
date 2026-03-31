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

NOTE: the work carried out by this skill does not require a work item, but the agent may optionally parse a work item token from the prompt to link the installation activity to a work item.

1. Establish current status

Run `wl plugins --json` to discover whether the AMPA plugin is currently installed or not.

If AMPA is currently installed AND the skill was activated with either an install or upgrade request display a message indicated that the installation will be upgraded using
the existing configuration and instructing the requestor to request to "Configure AMPA" if they wish to change the configuraiton.

If there is currently no installation or the skill was activated with a request to configure or change AMPA continue to step 2, otherwise skip to step 3.

2. Discord Bot Token

If a bot token was provided in the prompt that triggered this skill skip ahead to the next step.

Explain that a Discord bot token and channel ID are required for notifications from the AMPA agent and request the bot token and channel ID.

3. Install/Upgrade AMPA

Run the installer from the repository root providing any configuration options we have been given. If no options have been given then run the installer with only the --yes flag.

For example:

```
skill/install-ampa/scripts/install-worklog-plugin.sh --bot-token <discord_bot_token> --channel-id <discord_channel_id> --yes
```

### Installation Sources

The installer clones AMPA source code from the remote repository (`github.com/SorraTheOrc/ampa` by default). The installer does not provide a local or bundled-resource fallback for the Python package; a failure to clone from the remote repository is treated as a fatal error and the installer will exit with a clear diagnostic. Ensure network access and git are available when running the installer.

### Specifying a Version

To install a specific version or tag:

```
skill/install-ampa/scripts/install-worklog-plugin.sh --version v1.0.0 --yes
```

### Environment Variables

- `AMPA_REMOTE_REPO`: Override the default repository URL (default: `https://github.com/SorraTheOrc/ampa.git`)

### Error Handling

The installer handles network failures gracefully:
- Tests network connectivity before attempting clone
- Provides clear error messages for common failure scenarios and writes diagnostics to the decision log
- Does NOT fall back to local or bundled resources for the Python package; remote clone failures are reported and the install aborts

### Notes

- The script writes logs and decision traces under `/tmp` (e.g. `/tmp/ampa_install_decisions.<pid>` and `/tmp/ampa_install_*.log`).
- Git is required for remote repository cloning.
- Backward compatibility is maintained for existing configurations.
