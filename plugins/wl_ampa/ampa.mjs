// Node ESM implementation of the wl 'ampa' plugin, namespaced under plugins/wl_ampa
// Registers `wl ampa start|stop|status` and manages pid/log files under
// `.worklog/ampa/<name>.(pid|log)`.

import { spawn } from 'child_process';
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
      return { cmd: ['python', '-m', 'ampa.daemon'], env: { PYTHONPATH: pyPath } };
    }
  } catch (e) {}
  return null;
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

function writePid(ppath, pid) {
  fs.writeFileSync(ppath, String(pid), 'utf8');
}

async function start(projectRoot, cmd, name = 'default', foreground = false) {
  const ppath = pidPath(projectRoot, name);
  const lpath = logPath(projectRoot, name);
  if (fs.existsSync(ppath)) {
    try {
      const pid = parseInt(fs.readFileSync(ppath, 'utf8'), 10);
      if (isRunning(pid)) {
        console.log(`Already running (pid=${pid})`);
        return 0;
      }
    } catch (e) {}
  }
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
    writePid(ppath, proc.pid);
    proc.unref();
    console.log(`Started ${name} pid=${proc.pid} log=${lpath}`);
    return 0;
  } catch (e) {
    console.error('failed to start:', e && e.message ? e.message : e);
    return 1;
  }
}

async function stop(projectRoot, name = 'default', timeout = 10) {
  const ppath = pidPath(projectRoot, name);
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
    fs.unlinkSync(ppath);
    console.log('Not running (stale pid file cleared)');
    return 0;
  }
  try {
    try {
      process.kill(-pid, 'SIGTERM');
    } catch (e) {
      try { process.kill(pid, 'SIGTERM'); } catch (e2) {}
    }
  } catch (e) {}
  const start = Date.now();
  while (isRunning(pid) && Date.now() - start < timeout * 1000) {
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
    console.log('stopped');
    return 3;
  }
  let pid;
  try {
    pid = parseInt(fs.readFileSync(ppath, 'utf8'), 10);
  } catch (e) {
    try { fs.unlinkSync(ppath); } catch (e2) {}
    console.log('stopped (cleared corrupt pid file)');
    return 3;
  }
  if (isRunning(pid)) {
    console.log(`running pid=${pid} log=${lpath}`);
    return 0;
  } else {
    try { fs.unlinkSync(ppath); } catch (e) {}
    console.log('stopped (stale pid file removed)');
    return 3;
  }
}

export default function register(ctx) {
  const { program } = ctx;
  const ampa = program.command('ampa').description('Manage project dev daemons: start | stop | status');

  ampa
    .command('start')
    .description('Start the project daemon')
    .option('--cmd <cmd>', 'Command to run (overrides config)')
    .option('--name <name>', 'Daemon name', 'default')
    .option('--foreground', 'Run in foreground', false)
    .action(async (opts) => {
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const cmd = await resolveCommand(opts.cmd, cwd);
      if (!cmd) { console.error('No command resolved. Set --cmd, WL_AMPA_CMD or configure worklog.json/package.json/scripts.'); process.exitCode = 2; return; }
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
}
