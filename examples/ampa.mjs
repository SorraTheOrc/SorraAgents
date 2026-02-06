// Example ampa plugin to be installed into `.worklog/plugins/ampa.mjs`.
// This file is safe to keep in the repo and can be copied into the
// project-scoped plugin directory. It forwards `ampa` subcommands to the
// Python implementation located at `plugins.wl_ampa` using the system Python.

import { spawn } from 'child_process';

export default function register(ctx) {
  const { program, output, utils } = ctx;

  program
    .command('ampa')
    .description('Manage project dev daemons: start | stop | status')
    .argument('[args...]', 'ampa subcommand and options (e.g. start --name x --cmd "...")')
    .allowUnknownOption(true)
    .action((args = []) => {
      const py = process.env.WL_AMPA_PY || process.env.WL_PYTHON || 'python';
      const cmdArgs = ['-m', 'plugins.wl_ampa', ...args];

      try {
        const proc = spawn(py, cmdArgs, { stdio: 'inherit' });

        proc.on('error', (err) => {
          const msg = `failed to start python plugin bridge: ${err.message}`;
          if (utils && typeof utils.isJsonMode === 'function' && utils.isJsonMode()) {
            output.json({ success: false, error: msg });
          } else {
            console.error(msg);
          }
          process.exitCode = 1;
        });

        proc.on('close', (code) => {
          process.exitCode = code || 0;
        });
      } catch (err) {
        const msg = `exception while invoking python plugin bridge: ${err && err.message ? err.message : String(err)}`;
        if (utils && typeof utils.isJsonMode === 'function' && utils.isJsonMode()) {
          output.json({ success: false, error: msg });
        } else {
          console.error(msg);
        }
        process.exitCode = 1;
      }
    });
}
