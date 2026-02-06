import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'child_process';
import fs from 'fs';
import path from 'path';

// Lightweight lifecycle test for the Node ampa plugin when installed into
// .worklog/plugins in a temporary project directory.

test('ampa start/status/stop lifecycle', async (t) => {
  const tmp = path.join(process.cwd(), 'tmp-ampa-test');
  if (!fs.existsSync(tmp)) fs.mkdirSync(tmp);
  // create a simple test daemon script that traps SIGTERM and sleeps
  const daemon = path.join(tmp, 'test_daemon.js');
  fs.writeFileSync(
    daemon,
    `process.on('SIGTERM', ()=>{ console.log('got TERM'); process.exit(0); }); console.log('daemon running'); setInterval(()=>{},1000);`
  );
  fs.chmodSync(daemon, 0o755);
  // write worklog.json pointing to node daemon
  fs.writeFileSync(path.join(tmp, 'worklog.json'), JSON.stringify({ ampa: `node ${daemon}` }));

  // install the example plugin into tmp/.worklog/plugins
  const targetDir = path.join(tmp, '.worklog', 'plugins');
  fs.mkdirSync(targetDir, { recursive: true });
  fs.copyFileSync(path.join(process.cwd(), 'examples', 'ampa.mjs'), path.join(targetDir, 'ampa.mjs'));

  // run `node` to invoke the plugin directly via the ESM file (simulate wl loader)
  // Start
  await new Promise((resolve) => setTimeout(resolve, 50));
  const startProc = spawn('node', ['--input-type=module', path.join(targetDir, 'ampa.mjs'), 'start', '--name', 't1'], { cwd: tmp, stdio: 'inherit' });
  await new Promise((r) => startProc.on('close', r));

  // status
  const statusProc = spawn('node', ['--input-type=module', path.join(targetDir, 'ampa.mjs'), 'status', '--name', 't1'], { cwd: tmp, stdio: 'pipe' });
  let out = '';
  for await (const chunk of statusProc.stdout) out += chunk.toString();
  await new Promise((r) => statusProc.on('close', r));
  assert.ok(/running pid=\d+/.test(out), `status output unexpected: ${out}`);

  // stop
  const stopProc = spawn('node', ['--input-type=module', path.join(targetDir, 'ampa.mjs'), 'stop', '--name', 't1'], { cwd: tmp, stdio: 'inherit' });
  await new Promise((r) => stopProc.on('close', r));

  // cleanup
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (e) {}
});
