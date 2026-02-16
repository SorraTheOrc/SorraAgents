// Node ESM implementation of the wl 'ampa' plugin, moved into skill resources
// Registers `wl ampa start|stop|status|run` and manages pid/log files under
// `.worklog/ampa/<name>.(pid|log)`.

import { spawn, spawnSync } from 'child_process';
import fs from 'fs';
import fsPromises from 'fs/promises';
import path from 'path';

function findProjectRoot(start) {
  let cur = path.resolve(start);
  for (let i = 0; i < 100; i++) {
    if (
      fs.existsSync(path.join(cur, 'worklog.json')) ||
      fs.existsSync(path.join(cur, '.worklog')) ||
      fs.existsSync(path.join(cur, '.git'))
    ) {
      return cur;
    }
    const parent = path.dirname(cur);
    if (parent === cur) break;
    cur = parent;
  }
  throw new Error('project root not found (worklog.json, .worklog or .git)');
}

function shellSplit(s) {
  if (!s) return [];
  const re = /((?:\\.|[^\s"'])+)|"((?:\\.|[^\\"])*)"|'((?:\\.|[^\\'])*)'/g;
  const out = [];
  let m;
  while ((m = re.exec(s)) !== null) {
    out.push(m[1] || m[2] || m[3] || '');
  }
  return out;
}

function readDotEnvFile(envPath) {
  if (!envPath || !fs.existsSync(envPath)) return {};
  let content = '';
  try {
    content = fs.readFileSync(envPath, 'utf8');
  } catch (e) {
    return {};
  }
  const env = {};
  const lines = content.split(/\r?\n/);
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const normalized = trimmed.startsWith('export ') ? trimmed.slice(7).trim() : trimmed;
    const idx = normalized.indexOf('=');
    if (idx === -1) continue;
    const key = normalized.slice(0, idx).trim();
    let val = normalized.slice(idx + 1).trim();
    if (!key) continue;
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1);
    }
    env[key] = val;
  }
  return env;
}

function readDotEnv(projectRoot, extraPaths = []) {
  const envPaths = [path.join(projectRoot, '.env'), ...extraPaths];
  return envPaths.reduce((acc, envPath) => Object.assign(acc, readDotEnvFile(envPath)), {});
}

async function resolveCommand(cliCmd, projectRoot) {
  if (cliCmd) return Array.isArray(cliCmd) ? cliCmd : shellSplit(cliCmd);
  if (process.env.WL_AMPA_CMD) return shellSplit(process.env.WL_AMPA_CMD);
  const wl = path.join(projectRoot, 'worklog.json');
  if (fs.existsSync(wl)) {
    try {
      const data = JSON.parse(await fsPromises.readFile(wl, 'utf8'));
      if (data && typeof data === 'object' && 'ampa' in data) {
        const val = data.ampa;
        if (typeof val === 'string') return shellSplit(val);
        if (Array.isArray(val)) return val;
      }
    } catch (e) {}
  }
  const pkg = path.join(projectRoot, 'package.json');
  if (fs.existsSync(pkg)) {
    try {
      const pj = JSON.parse(await fsPromises.readFile(pkg, 'utf8'));
      const scripts = pj.scripts || {};
      if (scripts.ampa) return shellSplit(scripts.ampa);
    } catch (e) {}
  }
  const candidates = [path.join(projectRoot, 'scripts', 'ampa'), path.join(projectRoot, 'scripts', 'daemon')];
  for (const c of candidates) {
    try {
      if (fs.existsSync(c) && fs.accessSync(c, fs.constants.X_OK) === undefined) return [c];
    } catch (e) {}
  }
  // Fallback: if a bundled Python package 'ampa' was installed into
  // .worklog/plugins/ampa_py/ampa, prefer running it with Python -m ampa.daemon
  try {
    const pyBundle = path.join(projectRoot, '.worklog', 'plugins', 'ampa_py', 'ampa');
    if (fs.existsSync(path.join(pyBundle, '__init__.py'))) {
      const pyPath = path.join(projectRoot, '.worklog', 'plugins', 'ampa_py');
      const venvPython = path.join(pyPath, 'venv', 'bin', 'python');
      const pythonBin = fs.existsSync(venvPython) ? venvPython : 'python3';
      const launcher = `import sys; sys.path.insert(0, ${JSON.stringify(pyPath)}); import ampa.daemon as d; d.main()`;
      // Run the daemon in long-running mode by default (start scheduler).
      // Users can override via --cmd or AMPA_RUN_SCHEDULER env var if desired.
      // use -u to force unbuffered stdout/stderr so logs show up promptly
      return {
        cmd: [pythonBin, '-u', '-c', launcher, '--start-scheduler'],
        env: { PYTHONPATH: pyPath, AMPA_RUN_SCHEDULER: '1' },
      };
    }
  } catch (e) {}
  return null;
}

async function resolveRunOnceCommand(projectRoot, commandId) {
  if (!commandId) return null;
  // Prefer bundled python package if available.
  try {
    const pyBundle = path.join(projectRoot, '.worklog', 'plugins', 'ampa_py', 'ampa');
    if (fs.existsSync(path.join(pyBundle, '__init__.py'))) {
      const pyPath = path.join(projectRoot, '.worklog', 'plugins', 'ampa_py');
      const venvPython = path.join(pyPath, 'venv', 'bin', 'python');
      const pythonBin = fs.existsSync(venvPython) ? venvPython : 'python3';
      return {
        cmd: [pythonBin, '-u', '-m', 'ampa.scheduler', 'run-once', commandId],
        env: { PYTHONPATH: pyPath },
        envPaths: [path.join(pyPath, 'ampa', '.env')],
      };
    }
  } catch (e) {}
  // Fallback to repo/local package
  return {
    cmd: ['python3', '-m', 'ampa.scheduler', 'run-once', commandId],
    env: {},
    envPaths: [path.join(projectRoot, 'ampa', '.env')],
  };
}

function ensureDirs(projectRoot, name) {
  const base = path.join(projectRoot, '.worklog', 'ampa', name);
  fs.mkdirSync(base, { recursive: true });
  return base;
}

function pidPath(projectRoot, name) {
  return path.join(ensureDirs(projectRoot, name), `${name}.pid`);
}

function logPath(projectRoot, name) {
  return path.join(ensureDirs(projectRoot, name), `${name}.log`);
}

function isRunning(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    if (e && e.code === 'EPERM') return true;
    return false;
  }
}

function pidOwnedByProject(projectRoot, pid, lpath) {
  // Try /proc first (Linux). Fallback to ps if needed. Return true when a
  // substring that ties the process to this project is present in the cmdline.
  let cmdline = '';
  try {
    const p = `/proc/${pid}/cmdline`;
    if (fs.existsSync(p)) {
      cmdline = fs.readFileSync(p, 'utf8').replace(/\0/g, ' ').trim();
    }
  } catch (e) {}
  if (!cmdline) {
    try {
      const r = spawnSync('ps', ['-p', String(pid), '-o', 'args=']);
      if (r && r.status === 0 && r.stdout) cmdline = String(r.stdout).trim();
    } catch (e) {}
  }
  // Decide what patterns indicate ownership of the process by this project.
  const candidates = [
    projectRoot,
    path.join(projectRoot, '.worklog', 'plugins', 'ampa_py'),
    path.join(projectRoot, 'ampa'),
    'ampa.daemon',
    'ampa.scheduler',
  ];
  let matches = false;
  try {
    const lower = cmdline.toLowerCase();
    for (const c of candidates) {
      if (!c) continue;
      if (lower.includes(String(c).toLowerCase())) {
        matches = true;
        break;
      }
    }
  } catch (e) {}
  // Append a short diagnostic entry to the log if available.
  try {
    if (lpath) {
      fs.appendFileSync(lpath, `PID_VALIDATION pid=${pid} cmdline=${JSON.stringify(cmdline)} matches=${matches}\n`);
    }
  } catch (e) {}
  return matches;
}

function writePid(ppath, pid) {
  fs.writeFileSync(ppath, String(pid), 'utf8');
}

function readLogTail(lpath, maxBytes = 64 * 1024) {
  try {
    if (!fs.existsSync(lpath)) return '';
    const stat = fs.statSync(lpath);
    if (!stat || stat.size === 0) return '';
    const toRead = Math.min(stat.size, maxBytes);
    const fd = fs.openSync(lpath, 'r');
    const buf = Buffer.alloc(toRead);
    const pos = stat.size - toRead;
    fs.readSync(fd, buf, 0, toRead, pos);
    fs.closeSync(fd);
    return buf.toString('utf8');
  } catch (e) {
    return '';
  }
}

function extractErrorLines(text) {
  if (!text) return [];
  const lines = text.split(/\r?\n/);
  const re = /(ERROR|Traceback|Exception|AMPA_DISCORD_WEBHOOK)/i;
  const out = [];
  for (const l of lines) {
    if (re.test(l)) out.push(l);
  }
  // return last 200 matching lines at most
  return out.slice(-200);
}

function printLogErrors(lpath) {
  try {
    const tail = readLogTail(lpath);
    const errs = extractErrorLines(tail);
    if (errs.length > 0) {
      console.log('Recent errors from log:');
      for (const line of errs) console.log(line);
      return true;
    }
  } catch (e) {}
  return false;
}

function findMostRecentLog(projectRoot) {
  try {
    const base = path.join(projectRoot, '.worklog', 'ampa');
    if (!fs.existsSync(base)) return null;
    let best = { p: null, m: 0 };
    const names = fs.readdirSync(base);
    for (const n of names) {
      const sub = path.join(base, n);
      try {
        const st = fs.statSync(sub);
        if (!st.isDirectory()) continue;
      } catch (e) { continue; }
      const files = fs.readdirSync(sub);
      for (const f of files) {
        if (!f.endsWith('.log')) continue;
        const fp = path.join(sub, f);
        try {
          const s = fs.statSync(fp);
          if (s && s.mtimeMs > best.m) {
            best.p = fp;
            best.m = s.mtimeMs;
          }
        } catch (e) {}
      }
    }
    return best.p;
  } catch (e) {
    return null;
  }
}

async function start(projectRoot, cmd, name = 'default', foreground = false) {
  const ppath = pidPath(projectRoot, name);
  const lpath = logPath(projectRoot, name);
  if (fs.existsSync(ppath)) {
    try {
      const pid = parseInt(fs.readFileSync(ppath, 'utf8'), 10);
      if (isRunning(pid)) {
        // Verify the pid actually belongs to this project's ampa daemon
        const owned = pidOwnedByProject(projectRoot, pid, lpath);
        if (owned) {
          console.log(`Already running (pid=${pid})`);
          return 0;
        } else {
          try { fs.unlinkSync(ppath); } catch (e) {}
          console.log(`Stale pid file removed (pid=${pid} did not match project)`);
        }
      }
    } catch (e) {}
  }
  // Diagnostic: record the resolved command and env to the log so failures to
  // persist can be investigated easily.
  try {
    fs.appendFileSync(lpath, `Resolved command: ${JSON.stringify(cmd)}\n`);
  } catch (e) {}

  if (foreground) {
    if (cmd && cmd.cmd && Array.isArray(cmd.cmd)) {
      const env = Object.assign({}, process.env, cmd.env || {});
      const proc = spawn(cmd.cmd[0], cmd.cmd.slice(1), { cwd: projectRoot, stdio: 'inherit', env });
      return await new Promise((resolve) => {
        proc.on('exit', (code) => resolve(code || 0));
        proc.on('error', () => resolve(1));
      });
    }
    const proc = spawn(cmd[0], cmd.slice(1), { cwd: projectRoot, stdio: 'inherit' });
    return await new Promise((resolve) => {
      proc.on('exit', (code) => resolve(code || 0));
      proc.on('error', () => resolve(1));
    });
  }
  const out = fs.openSync(lpath, 'a');
  let proc;
  try {
    if (cmd && cmd.cmd && Array.isArray(cmd.cmd)) {
      const env = Object.assign({}, process.env, cmd.env || {});
      proc = spawn(cmd.cmd[0], cmd.cmd.slice(1), { cwd: projectRoot, detached: true, stdio: ['ignore', out, out], env });
    } else {
      proc = spawn(cmd[0], cmd.slice(1), { cwd: projectRoot, detached: true, stdio: ['ignore', out, out] });
    }
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    console.error('failed to start:', msg);
    // append the error message to the log file for easier diagnosis
    try { fs.appendFileSync(lpath, `Failed to spawn process: ${msg}\n`); } catch (ex) {}
    return 1;
  }
  if (!proc || !proc.pid) {
    console.error('failed to start: process did not spawn');
    return 1;
  }
  writePid(ppath, proc.pid);
  proc.unref();
  await new Promise((r) => setTimeout(r, 300));
  if (!isRunning(proc.pid)) {
    try { fs.unlinkSync(ppath); } catch (e) {}
    console.error('failed to start: process exited immediately');
    // Collect a helpful diagnostic snapshot: append the tail of the log to
    // the log itself with an explicit marker so operators can see what the
    // child process printed before exiting.
    try {
      const maxBytes = 32 * 1024; // read up to last 32KB of log
      const stat = fs.existsSync(lpath) && fs.statSync(lpath);
      if (stat && stat.size > 0) {
        const fd = fs.openSync(lpath, 'r');
        const toRead = Math.min(stat.size, maxBytes);
        const buf = Buffer.alloc(toRead);
        const pos = stat.size - toRead;
        fs.readSync(fd, buf, 0, toRead, pos);
        fs.closeSync(fd);
        fs.appendFileSync(lpath, `\n----- CHILD PROCESS OUTPUT (last ${toRead} bytes) -----\n`);
        fs.appendFileSync(lpath, buf.toString('utf8') + '\n');
        fs.appendFileSync(lpath, `----- END CHILD OUTPUT -----\n`);
      }
    } catch (ex) {
      try { fs.appendFileSync(lpath, `Failed to capture child output: ${String(ex)}\n`); } catch (e) {}
    }
    return 1;
  }
  console.log(`Started ${name} pid=${proc.pid} log=${lpath}`);
  return 0;
}

async function stop(projectRoot, name = 'default', timeout = 10) {
  const ppath = pidPath(projectRoot, name);
  const lpath = logPath(projectRoot, name);
  if (!fs.existsSync(ppath)) {
    console.log('Not running (no pid file)');
    return 0;
  }
  let pid;
  try {
    pid = parseInt(fs.readFileSync(ppath, 'utf8'), 10);
  } catch (e) {
    fs.unlinkSync(ppath);
    console.log('Stale pid file removed');
    return 0;
  }
  if (!isRunning(pid)) {
    try { fs.unlinkSync(ppath); } catch (e) {}
    console.log('Not running (stale pid file cleared)');
    return 0;
  }
  // Ensure the running pid is our process
  const owned = pidOwnedByProject(projectRoot, pid, lpath);
  if (!owned) {
    try { fs.unlinkSync(ppath); } catch (e) {}
    console.log('Not running (pid belonged to another process)');
    return 0;
  }
  try {
    try {
      process.kill(-pid, 'SIGTERM');
    } catch (e) {
      try { process.kill(pid, 'SIGTERM'); } catch (e2) {}
    }
  } catch (e) {}
  const startTime = Date.now();
  while (isRunning(pid) && Date.now() - startTime < timeout * 1000) {
    await new Promise((r) => setTimeout(r, 100));
  }
  if (isRunning(pid)) {
    try {
      try { process.kill(-pid, 'SIGKILL'); } catch (e) { process.kill(pid, 'SIGKILL'); }
    } catch (e) {}
  }
  if (!isRunning(pid)) {
    try { fs.unlinkSync(ppath); } catch (e) {}
    console.log(`Stopped pid=${pid}`);
    return 0;
  }
  console.log(`Failed to stop pid=${pid}`);
  return 1;
}

async function status(projectRoot, name = 'default') {
  const ppath = pidPath(projectRoot, name);
  const lpath = logPath(projectRoot, name);
  if (!fs.existsSync(ppath)) {
    // Even when there's no pidfile, the daemon may have started and exited
    // quickly with an error recorded in the log. Surface any recent errors
    // so `wl ampa status` provides helpful diagnostics. If the current
    // daemon log path isn't present (no pidfile), attempt to find the most
    // recent log under .worklog/ampa and show errors from there.
    const alt = findMostRecentLog(projectRoot) || lpath;
    try { printLogErrors(alt); } catch (e) {}
    console.log('stopped');
    return 3;
  }
  let pid;
  try {
    pid = parseInt(fs.readFileSync(ppath, 'utf8'), 10);
  } catch (e) {
    try { fs.unlinkSync(ppath); } catch (e2) {}
    const alt = findMostRecentLog(projectRoot) || lpath;
    try { printLogErrors(alt); } catch (e) {}
    console.log('stopped (cleared corrupt pid file)');
    return 3;
  }
    if (isRunning(pid)) {
    // verify ownership before reporting running
    const owned = pidOwnedByProject(projectRoot, pid, lpath);
    if (owned) {
      console.log(`running pid=${pid} log=${lpath}`);
      return 0;
    } else {
      try { fs.unlinkSync(ppath); } catch (e) {}
      console.log('stopped (stale pid file removed)');
      return 3;
    }
  } else {
    try { fs.unlinkSync(ppath); } catch (e) {}
    const alt = findMostRecentLog(projectRoot) || lpath;
    try { printLogErrors(alt); } catch (e) {}
    console.log('stopped (stale pid file removed)');
    return 3;
  }
}

async function runOnce(projectRoot, cmdSpec) {
  const envPaths = cmdSpec && Array.isArray(cmdSpec.envPaths) ? cmdSpec.envPaths : [];
  const dotenvEnv = readDotEnv(projectRoot, envPaths);
  if (cmdSpec && cmdSpec.cmd && Array.isArray(cmdSpec.cmd)) {
    const env = Object.assign({}, process.env, dotenvEnv, cmdSpec.env || {});
    const proc = spawn(cmdSpec.cmd[0], cmdSpec.cmd.slice(1), { cwd: projectRoot, stdio: 'inherit', env });
    return await new Promise((resolve) => {
      proc.on('exit', (code) => resolve(code || 0));
      proc.on('error', () => resolve(1));
    });
  }
  return 1;
}

export default function register(ctx) {
  const { program } = ctx;
  const ampa = program.command('ampa').description('Manage project dev daemons: start | stop | status | run');

  ampa
    .command('start')
    .description('Start the project daemon')
    .option('--cmd <cmd>', 'Command to run (overrides config)')
    .option('--name <name>', 'Daemon name', 'default')
    .option('--foreground', 'Run in foreground', false)
    .option('--verbose', 'Print resolved command and env', false)
    .action(async (opts) => {
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const cmd = await resolveCommand(opts.cmd, cwd);
      if (!cmd) {
        console.error('No command resolved. Set --cmd, WL_AMPA_CMD or configure worklog.json/package.json/scripts.');
        process.exitCode = 2;
        return;
      }
      if (opts.verbose) {
        try {
          if (cmd && cmd.cmd && Array.isArray(cmd.cmd)) {
            console.log('Resolved command:', cmd.cmd.join(' '), 'env:', JSON.stringify(cmd.env || {}));
          } else if (Array.isArray(cmd)) {
            console.log('Resolved command:', cmd.join(' '));
          } else {
            console.log('Resolved command (unknown form):', JSON.stringify(cmd));
          }
        } catch (e) {}
      }
      const code = await start(cwd, cmd, opts.name, opts.foreground);
      process.exitCode = code;
    });

  ampa
    .command('stop')
    .description('Stop the project daemon')
    .option('--name <name>', 'Daemon name', 'default')
    .action(async (opts) => {
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const code = await stop(cwd, opts.name);
      process.exitCode = code;
    });

  ampa
    .command('status')
    .description('Show daemon status')
    .option('--name <name>', 'Daemon name', 'default')
    .action(async (opts) => {
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const code = await status(cwd, opts.name);
      process.exitCode = code;
    });

  ampa
    .command('run')
    .description('Run a scheduler command immediately by id')
    .arguments('<command-id>')
    .action(async (commandId) => {
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const cmdSpec = await resolveRunOnceCommand(cwd, commandId);
      if (!cmdSpec) {
        console.error('No run-once command resolved.');
        process.exitCode = 2;
        return;
      }
      const code = await runOnce(cwd, cmdSpec);
      process.exitCode = code;
    });


}
