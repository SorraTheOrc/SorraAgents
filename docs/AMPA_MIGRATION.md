# AMPA Migration Guide

This guide helps existing users transition from the old bundled AMPA installation to the new repository-based installation.

## What Changed

AMPA (Automated Project Management Agent) has been separated from the OpenCode repository into its own independent repository at **https://github.com/opencode/ampa**.

### Before
- AMPA source code was bundled in `skill/install-ampa/resources/ampa_py/`
- Development was done within the OpenCode repository
- The `ampa/` directory at the repository root contained source files

### After
- AMPA source code lives exclusively in the new repository
- The installer clones from the remote repository at installation time
- AMPA is now an independent project with its own CI/CD pipeline

## Who Needs to Migrate

You need to take action if:
- You were developing AMPA code in the `ampa/` directory
- You have local modifications to AMPA source files
- You were referencing AMPA internals in your projects
- You had a local AMPA installation from the bundled resources

You do **not** need to migrate if:
- You only use AMPA through the `wl ampa` commands
- You installed AMPA using the standard installer

## Migration Steps

### 1. Backup Any Local Changes

If you have uncommitted changes in the old `ampa/` directory, back them up now:

```bash
# If you still have the old ampa/ directory
cp -r ampa/ ~/ampa-backup-$(date +%Y%m%d)
```

### 2. Install the Latest AMPA

Run the installer to get the latest AMPA version from the new repository:

```bash
skill/install-ampa/scripts/install-worklog-plugin.sh --yes
```

The installer will:
- Clone the AMPA repository from GitHub
- Install the latest stable version
- Maintain your existing configuration

### 3. Verify the Installation

Check that AMPA is working correctly:

```bash
wl plugins --json | grep ampa
wl ampa --help
```

### 4. Update Your Development Workflow

If you were developing AMPA:

1. **Clone the new repository** (instead of editing in OpenCode):
   ```bash
   git clone https://github.com/opencode/ampa.git ~/ampa-dev
   cd ~/ampa-dev
   ```

2. **Follow the AMPA repository's development guidelines**:
   - Read the AMPA README.md for setup instructions
   - Run tests in the AMPA repository
   - Submit PRs to the AMPA repository, not OpenCode

3. **Test your changes**:
   ```bash
   # In the AMPA repository
   npm test  # or equivalent test command
   ```

4. **Re-install to test locally**:
   ```bash
   # From OpenCode repository
   skill/install-ampa/scripts/install-worklog-plugin.sh --yes
   ```

## Configuration Preservation

Your existing AMPA configuration is preserved during migration:

- **Global config**: `~/.config/opencode/.worklog/ampa/`
- **Per-project config**: `<project>/.worklog/ampa/`
- **Discord bot tokens**: Stored in `.env` files (not affected by migration)
- **Scheduler state**: `scheduler_store.json` persists

## Troubleshooting

### Issue: AMPA commands not found after migration

**Solution**: Re-install AMPA:
```bash
skill/install-ampa/scripts/install-worklog-plugin.sh --yes
```

### Issue: Missing AMPA source files

**Solution**: This is expected. AMPA source is no longer in the OpenCode repository. Clone the new repository if you need source access:
```bash
git clone https://github.com/opencode/ampa.git
```

### Issue: Local modifications lost

**Solution**: If you had local changes in `skill/install-ampa/resources/ampa_py/` or `ampa/`:
1. Check if you have a backup
2. Apply your changes to the new AMPA repository
3. Consider submitting them as PRs to benefit the community

### Issue: Tests failing after migration

**Solution**: AMPA tests have moved to the AMPA repository. Run them there:
```bash
cd ~/ampa  # or wherever you cloned it
npm test
```

## For Contributors

### Submitting AMPA Changes

All AMPA development now happens in the dedicated repository:

1. Fork https://github.com/opencode/ampa
2. Create a feature branch
3. Make your changes
4. Run AMPA tests in that repository
5. Submit a PR to the AMPA repository

### Updating OpenCode Documentation

If you find OpenCode documentation that still references the old AMPA locations:

1. Open an issue in the OpenCode repository
2. Reference this migration guide
3. The documentation will be updated to point to the new repository

## Timeline

- **Completed**: AMPA code migrated to new repository
- **Completed**: OpenCode documentation updated with new repository links
- **Completed**: Installer updated to clone from remote repository
- **You are here**: Migration guide published

## Getting Help

- **AMPA Issues**: https://github.com/opencode/ampa/issues
- **OpenCode Issues**: https://github.com/opencode/opencode/issues
- **Migration Questions**: Open an issue in either repository with "migration" in the title

## References

- AMPA Repository: https://github.com/opencode/ampa
- AMPA README: https://github.com/opencode/ampa/blob/main/README.md
- OpenCode README: [../README.md](../README.md)
- Installation Skill: [../skill/install-ampa/SKILL.md](../skill/install-ampa/SKILL.md)
