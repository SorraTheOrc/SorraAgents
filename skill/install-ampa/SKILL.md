---
name: Install AMPA Skill
description: |
  Install and maintain the ampa Worklog plugin by running the bundled installer script.
---

## Purpose

Provide a simple, canonical location for the ampa installer so agents and operators can run it as a skill.

## Usage

Run the installer from the repository root:

```
skill/install-ampa/scripts/install-worklog-plugin.sh [--webhook <url>] [--yes] [--restart|--no-restart] [source-file] [target-dir]
```

Examples:

```
# non-interactive install using the default source
skill/install-ampa/scripts/install-worklog-plugin.sh --yes

# install with explicit source and target
skill/install-ampa/scripts/install-worklog-plugin.sh plugins/wl_ampa/ampa.mjs .worklog/plugins
```

Notes:
- This skill does not post Worklog audit comments by default and does not accept or require a work-item id.
- The script writes logs and decision traces under `/tmp` (e.g. `/tmp/ampa_install_decisions.<pid>` and `/tmp/ampa_install_*.log`).
