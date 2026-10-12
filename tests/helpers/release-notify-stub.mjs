/**
 * Shared test-isolation helper for the ship release harnesses
 * (WL-0MUYA98Z8003HU8X / SA-0MU3VIW8C0035KKL).
 *
 * The release harnesses run the REAL `run-release.js` inside a temp skill
 * layout. If that layout contains the REAL `discord-notify.js` and the temp
 * project has no `.worklog/` webhook config, Step 8.5 falls back to the
 * operator's global `~/.pi/agent/config.yaml` webhook and POSTs a real
 * `Release v<version>` Discord notification on every test run.
 *
 * Tests must never perform real outbound notifications, so every harness that
 * does not deliberately observe a loopback POST writes this recording stub in
 * place of the real module. The stub performs no network I/O and records each
 * Step 8.5 call (as a JSON line) to `$DISCORD_NOTIFY_LOG`, so a test can pin
 * that the stub — not the real module — is the code path that ran.
 *
 * Use `writeStubDiscordNotify(skillScriptDir)` to install the stub, and set
 * the child process env `DISCORD_NOTIFY_LOG` to a temp-file path to capture
 * calls.
 */

import { writeFileSync } from 'node:fs';
import { join } from 'node:path';

export const DISCORD_NOTIFY_LOG_ENV = 'DISCORD_NOTIFY_LOG';

/**
 * Source of the recording stub written in place of `discord-notify.js`.
 * It mirrors the real module's public surface (`sendReleaseNotification`) but
 * never touches the network.
 */
export const STUB_DISCORD_NOTIFY_SOURCE = `// Recording stub — no network I/O.
export async function sendReleaseNotification({ version, prUrl, projectRoot } = {}) {
  const logPath = process.env.${DISCORD_NOTIFY_LOG_ENV};
  if (logPath) {
    const { appendFileSync } = await import('node:fs');
    appendFileSync(
      logPath,
      JSON.stringify({ version, prUrl, projectRoot, stubbed: true }) + '\\n',
    );
  }
  return { success: true, notified: false, stubbed: true };
}
`;

/**
 * Write the recording stub as `discord-notify.js` in a harness's temp skill
 * script directory.
 *
 * @param {string} skillScriptDir - Temp skill `scripts/` directory.
 * @returns {string} The path of the written stub module.
 */
export function writeStubDiscordNotify(skillScriptDir) {
  const stubPath = join(skillScriptDir, 'discord-notify.js');
  writeFileSync(stubPath, STUB_DISCORD_NOTIFY_SOURCE);
  return stubPath;
}
