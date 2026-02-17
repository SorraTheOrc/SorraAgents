// Node ESM implementation of the wl 'ampa' plugin, moved into skill resources
// Registers `wl ampa start|stop|status|run|list|ls|start-work|finish-work|list-containers`
// and manages pid/log files under `.worklog/ampa/<name>.(pid|log)`.

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

async function resolveListCommand(projectRoot, useJson) {
  const args = ['-m', 'ampa.scheduler', 'list'];
  if (useJson) args.push('--json');
  // Prefer bundled python package if available.
  try {
    const pyBundle = path.join(projectRoot, '.worklog', 'plugins', 'ampa_py', 'ampa');
    if (fs.existsSync(path.join(pyBundle, '__init__.py'))) {
      const pyPath = path.join(projectRoot, '.worklog', 'plugins', 'ampa_py');
      const venvPython = path.join(pyPath, 'venv', 'bin', 'python');
      const pythonBin = fs.existsSync(venvPython) ? venvPython : 'python3';
      return {
        cmd: [pythonBin, '-u', ...args],
        env: { PYTHONPATH: pyPath },
        envPaths: [path.join(pyPath, 'ampa', '.env')],
      };
    }
  } catch (e) {}
  return {
    cmd: ['python3', '-u', ...args],
    env: {},
    envPaths: [path.join(projectRoot, 'ampa', '.env')],
  };
}

const DAEMON_NOT_RUNNING_MESSAGE = 'Daemon is not running. Start it with: wl ampa start';

function readDaemonEnv(pid) {
  try {
    const envRaw = fs.readFileSync(`/proc/${pid}/environ`, 'utf8');
    const out = {};
    for (const entry of envRaw.split('\0')) {
      if (!entry) continue;
      const idx = entry.indexOf('=');
      if (idx === -1) continue;
      const key = entry.slice(0, idx);
      const val = entry.slice(idx + 1);
      out[key] = val;
    }
    return out;
  } catch (e) {
    return null;
  }
}

function resolveDaemonStore(projectRoot, name = 'default') {
  const ppath = pidPath(projectRoot, name);
  if (!fs.existsSync(ppath)) return { running: false };
  let pid;
  try {
    pid = parseInt(fs.readFileSync(ppath, 'utf8'), 10);
  } catch (e) {
    return { running: false };
  }
  if (!isRunning(pid)) return { running: false };
  const owned = pidOwnedByProject(projectRoot, pid, logPath(projectRoot, name));
  if (!owned) return { running: false };

  let cwd = projectRoot;
  try {
    cwd = fs.readlinkSync(`/proc/${pid}/cwd`);
  } catch (e) {}
  const env = readDaemonEnv(pid) || {};
  let storePath = env.AMPA_SCHEDULER_STORE || '';
  if (!storePath) {
    const candidates = [];
    if (env.PYTHONPATH) {
      for (const entry of env.PYTHONPATH.split(path.delimiter)) {
        if (entry) candidates.push(entry);
      }
    }
    candidates.push(path.join(projectRoot, '.worklog', 'plugins', 'ampa_py'));
    for (const candidate of candidates) {
      const ampaPath = path.join(candidate, 'ampa');
      if (fs.existsSync(path.join(ampaPath, 'scheduler.py'))) {
        storePath = path.join(ampaPath, 'scheduler_store.json');
        break;
      }
    }
  }
  if (!storePath) {
    storePath = path.join(cwd, 'ampa', 'scheduler_store.json');
  } else if (!path.isAbsolute(storePath)) {
    storePath = path.resolve(cwd, storePath);
  }
  return { running: true, pid, cwd, env, storePath };
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

// ---------------------------------------------------------------------------
// Dev container helpers (start-work / finish-work / list-containers)
// ---------------------------------------------------------------------------

const CONTAINER_IMAGE = 'ampa-dev:latest';
const CONTAINER_PREFIX = 'ampa-';

/**
 * Check if a binary exists in $PATH. Returns true if found, false otherwise.
 */
function checkBinary(name) {
  const whichCmd = process.platform === 'win32' ? 'where' : 'which';
  const result = spawnSync(whichCmd, [name], { stdio: 'pipe' });
  return result.status === 0;
}

/**
 * Check that all required binaries (podman, distrobox, git, wl) are available.
 * Returns an object with { ok, missing } where missing is an array of names.
 */
function checkPrerequisites() {
  const required = ['podman', 'distrobox', 'git', 'wl'];
  const missing = required.filter((bin) => !checkBinary(bin));
  return { ok: missing.length === 0, missing };
}

/**
 * Validate a work item exists via `wl show <id> --json`.
 * Returns the work item data on success, or null on failure.
 */
function validateWorkItem(id) {
  const result = spawnSync('wl', ['show', id, '--json'], { stdio: 'pipe', encoding: 'utf8' });
  if (result.status !== 0) return null;
  try {
    const parsed = JSON.parse(result.stdout);
    if (parsed && parsed.success && parsed.workItem) return parsed.workItem;
    return null;
  } catch (e) {
    return null;
  }
}

/**
 * Check if a Podman container with the given name already exists.
 */
function checkContainerExists(name) {
  const result = spawnSync('podman', ['container', 'exists', name], { stdio: 'pipe' });
  return result.status === 0;
}

/**
 * Get the git remote origin URL from the current directory.
 * Returns the URL string or null if not available.
 */
function getGitOrigin() {
  const result = spawnSync('git', ['remote', 'get-url', 'origin'], { stdio: 'pipe', encoding: 'utf8' });
  if (result.status !== 0 || !result.stdout) return null;
  return result.stdout.trim();
}

/**
 * Derive a container name from a work item ID.
 */
function containerName(workItemId) {
  return `${CONTAINER_PREFIX}${workItemId}`;
}

/**
 * Derive a branch name from a work item's issue type and ID.
 * Pattern: <issueType>/<work-item-id>
 * Falls back to task/ if issueType is unknown or empty.
 */
function branchName(workItemId, issueType) {
  const validTypes = ['feature', 'bug', 'chore', 'task'];
  const type = issueType && validTypes.includes(issueType) ? issueType : 'task';
  return `${type}/${workItemId}`;
}

/**
 * Check if the Podman image exists locally.
 */
function imageExists(imageName) {
  const result = spawnSync('podman', ['image', 'exists', imageName], { stdio: 'pipe' });
  return result.status === 0;
}

/**
 * Build the Podman image from the Containerfile.
 * Returns { ok, message }.
 */
function buildImage(projectRoot) {
  const containerfilePath = path.join(projectRoot, 'ampa', 'Containerfile');
  if (!fs.existsSync(containerfilePath)) {
    return { ok: false, message: `Containerfile not found at ${containerfilePath}` };
  }
  console.log(`Building image ${CONTAINER_IMAGE} from ${containerfilePath}...`);
  const result = spawnSync('podman', ['build', '-t', CONTAINER_IMAGE, '-f', containerfilePath, path.join(projectRoot, 'ampa')], {
    stdio: 'inherit',
  });
  if (result.status !== 0) {
    return { ok: false, message: `podman build failed with exit code ${result.status}` };
  }
  return { ok: true, message: 'Image built successfully' };
}

/**
 * Run a command synchronously, returning { status, stdout, stderr }.
 */
function runSync(cmd, args, opts = {}) {
  const result = spawnSync(cmd, args, { encoding: 'utf8', stdio: 'pipe', ...opts });
  return {
    status: result.status,
    stdout: (result.stdout || '').trim(),
    stderr: (result.stderr || '').trim(),
  };
}

/**
 * Create and enter a Distrobox container for a work item.
 */
async function startWork(projectRoot, workItemId, agentName) {
  // 1. Check prerequisites
  const prereqs = checkPrerequisites();
  if (!prereqs.ok) {
    const installHints = {
      podman: 'Install podman: https://podman.io/getting-started/installation',
      distrobox: 'Install distrobox: https://github.com/89luca89/distrobox#installation',
      git: 'Install git: apt install git / brew install git',
      wl: 'Install wl: see project README',
    };
    console.error(`Missing required tools: ${prereqs.missing.join(', ')}`);
    for (const m of prereqs.missing) {
      if (installHints[m]) console.error(`  ${installHints[m]}`);
    }
    return 2;
  }

  // 2. Validate work item
  const workItem = validateWorkItem(workItemId);
  if (!workItem) {
    console.error(`Work item "${workItemId}" not found. Verify the ID with: wl show ${workItemId}`);
    return 2;
  }

  // 3. Check for existing container
  const cName = containerName(workItemId);
  if (checkContainerExists(cName)) {
    console.error(`Container "${cName}" already exists. Use 'wl ampa list-containers' to inspect or 'wl ampa finish-work' to clean up.`);
    return 2;
  }

  // 4. Get git origin
  const origin = getGitOrigin();
  if (!origin) {
    console.error('Could not determine git remote origin. Ensure this is a git repo with a remote named "origin".');
    return 2;
  }

  // 5. Build image if needed
  if (!imageExists(CONTAINER_IMAGE)) {
    const build = buildImage(projectRoot);
    if (!build.ok) {
      console.error(`Failed to build container image: ${build.message}`);
      return 2;
    }
  }

  // 6. Derive branch name
  const branch = branchName(workItemId, workItem.issueType);

  // 7. Create Distrobox container
  console.log(`Creating container "${cName}" from image ${CONTAINER_IMAGE}...`);
  const createResult = runSync('distrobox', [
    'create',
    '--name', cName,
    '--image', CONTAINER_IMAGE,
    '--yes',
    '--no-entry',
  ]);
  if (createResult.status !== 0) {
    console.error(`Failed to create container: ${createResult.stderr || createResult.stdout}`);
    return 1;
  }

  // 8. Run setup inside the container:
  //    - Clone the project (shallow)
  //    - Create/checkout branch
  //    - Run wl init + wl sync
  const setupScript = [
    `set -e`,
    `cd /workdir`,
    `echo "Cloning project from ${origin}..."`,
    `git clone --depth 1 "${origin}" project`,
    `cd project`,
    // Check if branch exists on remote
    `if git ls-remote --heads origin "${branch}" | grep -q "${branch}"; then`,
    `  echo "Branch ${branch} exists on remote, checking out..."`,
    `  git fetch origin "${branch}" --depth 1`,
    `  git checkout -b "${branch}" "origin/${branch}"`,
    `else`,
    `  echo "Creating new branch ${branch}..."`,
    `  git checkout -b "${branch}"`,
    `fi`,
    // Set environment variables for container detection
    `echo "export AMPA_CONTAINER_NAME=${cName}" >> ~/.bashrc`,
    `echo "export AMPA_WORK_ITEM_ID=${workItemId}" >> ~/.bashrc`,
    `echo "export AMPA_BRANCH=${branch}" >> ~/.bashrc`,
    // Initialize worklog
    `if command -v wl >/dev/null 2>&1; then`,
    `  echo "Initializing worklog..."`,
    `  wl init --json || echo "wl init skipped (may already be initialized)"`,
    `  wl sync || echo "wl sync skipped"`,
    `else`,
    `  echo "Warning: wl not found in PATH. Worklog will not be initialized."`,
    `fi`,
    `echo "Setup complete. Project cloned to /workdir/project on branch ${branch}"`,
  ].join('\n');

  console.log('Running setup inside container...');
  const setupResult = runSync('distrobox', [
    'enter', cName, '--', 'bash', '-c', setupScript,
  ]);
  if (setupResult.status !== 0) {
    console.error(`Container setup failed: ${setupResult.stderr || setupResult.stdout}`);
    // Attempt cleanup
    spawnSync('distrobox', ['rm', '--force', cName], { stdio: 'pipe' });
    return 1;
  }
  if (setupResult.stdout) console.log(setupResult.stdout);

  // 9. Claim work item if agent name provided
  if (agentName) {
    spawnSync('wl', ['update', workItemId, '--status', 'in_progress', '--assignee', agentName, '--json'], {
      stdio: 'pipe',
      encoding: 'utf8',
    });
  }

  // 10. Enter the container interactively
  console.log(`\nEntering container "${cName}"...`);
  console.log(`Work directory: /workdir/project`);
  console.log(`Branch: ${branch}`);
  console.log(`Work item: ${workItemId} - ${workItem.title}`);
  console.log(`\nRun 'wl ampa finish-work' when done.\n`);

  const enterProc = spawn('distrobox', ['enter', cName, '--', 'bash', '--login', '-c', 'cd /workdir/project && exec bash --login'], {
    stdio: 'inherit',
  });

  return new Promise((resolve) => {
    enterProc.on('exit', (code) => resolve(code || 0));
    enterProc.on('error', (err) => {
      console.error(`Failed to enter container: ${err.message}`);
      resolve(1);
    });
  });
}

/**
 * Finish work in a dev container: commit, push, update work item, destroy container.
 */
async function finishWork(force = false) {
  // 1. Detect container context
  const cName = process.env.AMPA_CONTAINER_NAME;
  const workItemId = process.env.AMPA_WORK_ITEM_ID;
  const branch = process.env.AMPA_BRANCH;

  if (!cName || !workItemId) {
    console.error('Not running inside a start-work container. Set AMPA_CONTAINER_NAME and AMPA_WORK_ITEM_ID or use this command from within a container created by "wl ampa start-work".');
    return 2;
  }

  // 2. Check for uncommitted changes
  const statusResult = runSync('git', ['status', '--porcelain']);
  const hasUncommitted = statusResult.stdout.length > 0;

  if (hasUncommitted && !force) {
    // Commit and push
    console.log('Uncommitted changes detected. Committing...');
    const addResult = runSync('git', ['add', '-A']);
    if (addResult.status !== 0) {
      console.error(`git add failed: ${addResult.stderr}`);
      return 1;
    }

    const commitMsg = `${workItemId}: Work completed in dev container`;
    const commitResult = runSync('git', ['commit', '-m', commitMsg]);
    if (commitResult.status !== 0) {
      console.error(`git commit failed: ${commitResult.stderr}`);
      console.error('Uncommitted files:');
      console.error(statusResult.stdout);
      console.error('Use --force to destroy the container without committing (changes will be lost).');
      return 1;
    }
    console.log(commitResult.stdout);
  } else if (hasUncommitted && force) {
    console.log('Warning: Discarding uncommitted changes (--force)');
    console.log(statusResult.stdout);
  }

  // 3. Push if there are commits to push
  if (!force) {
    const pushBranch = branch || 'HEAD';
    console.log(`Pushing ${pushBranch} to origin...`);
    const pushResult = runSync('git', ['push', '-u', 'origin', pushBranch]);
    if (pushResult.status !== 0) {
      console.error(`git push failed: ${pushResult.stderr}`);
      console.error('Use --force to destroy the container without pushing.');
      return 1;
    }
    if (pushResult.stdout) console.log(pushResult.stdout);

    // Get the last commit hash for the work item comment
    const hashResult = runSync('git', ['rev-parse', '--short', 'HEAD']);
    const commitHash = hashResult.stdout || 'unknown';

    // 4. Update work item
    console.log(`Updating work item ${workItemId}...`);
    spawnSync('wl', ['update', workItemId, '--stage', 'in_review', '--status', 'completed', '--json'], {
      stdio: 'pipe',
      encoding: 'utf8',
    });
    spawnSync('wl', ['comment', 'add', workItemId, '--comment', `Work completed in dev container ${cName}. Branch: ${pushBranch}. Latest commit: ${commitHash}`, '--author', 'ampa', '--json'], {
      stdio: 'pipe',
      encoding: 'utf8',
    });
  }

  // 5. Exit and destroy container
  console.log(`Destroying container "${cName}"...`);
  // We need to exit the container first, then destroy from the host.
  // When running inside the container, we signal completion and the
  // host-side caller handles destruction.
  // Write a marker file that the host can check.
  const markerDir = path.join(process.env.HOME || '/tmp', '.ampa');
  try {
    fs.mkdirSync(markerDir, { recursive: true });
    fs.writeFileSync(path.join(markerDir, `${cName}.done`), JSON.stringify({
      workItemId,
      branch,
      timestamp: new Date().toISOString(),
      force,
    }));
  } catch (e) {
    // Non-fatal: marker is just a convenience
  }

  console.log(`Container "${cName}" marked for cleanup.`);
  console.log('Run the following from the host to destroy the container:');
  console.log(`  distrobox rm --force ${cName}`);

  return 0;
}

/**
 * List all dev containers created by start-work.
 */
function listContainers(useJson = false) {
  // Parse output of podman ps to find ampa-* containers
  const result = runSync('podman', ['ps', '-a', '--filter', `name=${CONTAINER_PREFIX}`, '--format', '{{.Names}}\\t{{.Status}}\\t{{.Created}}']);
  if (result.status !== 0) {
    // podman might not be installed
    if (!checkBinary('podman')) {
      console.error('podman is not installed. Install podman: https://podman.io/getting-started/installation');
      return 2;
    }
    console.error(`Failed to list containers: ${result.stderr}`);
    return 1;
  }

  const lines = result.stdout.split('\n').filter(Boolean);
  const containers = lines.map((line) => {
    const parts = line.split('\t');
    const name = parts[0] || '';
    const status = parts[1] || 'unknown';
    const created = parts[2] || 'unknown';
    // Extract work item ID from container name (ampa-<work-item-id>)
    const workItemIdMatch = name.startsWith(CONTAINER_PREFIX) ? name.slice(CONTAINER_PREFIX.length) : null;
    return { name, workItemId: workItemIdMatch, status, created };
  });

  if (useJson) {
    console.log(JSON.stringify({ containers }, null, 2));
  } else if (containers.length === 0) {
    console.log('No dev containers found.');
  } else {
    console.log('Dev containers:');
    console.log(`${'NAME'.padEnd(40)} ${'WORK ITEM'.padEnd(24)} ${'STATUS'.padEnd(20)} CREATED`);
    console.log('-'.repeat(100));
    for (const c of containers) {
      console.log(`${c.name.padEnd(40)} ${(c.workItemId || '-').padEnd(24)} ${c.status.padEnd(20)} ${c.created}`);
    }
  }

  return 0;
}

export default function register(ctx) {
  const { program } = ctx;
  const ampa = program.command('ampa').description('Manage project dev daemons and dev containers');

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

  ampa
    .command('list')
    .description('List scheduled commands')
    .option('--json', 'Output JSON')
    .option('--name <name>', 'Daemon name', 'default')
    .option('--verbose', 'Print resolved store path', false)
    .action(async (opts) => {
      const verbose = !!opts.verbose || process.argv.includes('--verbose');
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const daemon = resolveDaemonStore(cwd, opts.name);
      if (!daemon.running) {
        console.log(DAEMON_NOT_RUNNING_MESSAGE);
        process.exitCode = 3;
        return;
      }
      const cmdSpec = await resolveListCommand(cwd, !!opts.json);
      if (!cmdSpec) {
        console.error('No list command resolved.');
        process.exitCode = 2;
        return;
      }
      if (daemon.storePath) {
        if (verbose) {
          console.log(`Using scheduler store: ${daemon.storePath}`);
        }
        cmdSpec.env = Object.assign({}, cmdSpec.env || {}, { AMPA_SCHEDULER_STORE: daemon.storePath });
      }
      const code = await runOnce(cwd, cmdSpec);
      process.exitCode = code;
    });

  ampa
    .command('ls')
    .description('Alias for list')
    .option('--json', 'Output JSON')
    .option('--name <name>', 'Daemon name', 'default')
    .option('--verbose', 'Print resolved store path', false)
    .action(async (opts) => {
      const verbose = !!opts.verbose || process.argv.includes('--verbose');
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const daemon = resolveDaemonStore(cwd, opts.name);
      if (!daemon.running) {
        console.log(DAEMON_NOT_RUNNING_MESSAGE);
        process.exitCode = 3;
        return;
      }
      const cmdSpec = await resolveListCommand(cwd, !!opts.json);
      if (!cmdSpec) {
        console.error('No list command resolved.');
        process.exitCode = 2;
        return;
      }
      if (daemon.storePath) {
        if (verbose) {
          console.log(`Using scheduler store: ${daemon.storePath}`);
        }
        cmdSpec.env = Object.assign({}, cmdSpec.env || {}, { AMPA_SCHEDULER_STORE: daemon.storePath });
      }
      const code = await runOnce(cwd, cmdSpec);
      process.exitCode = code;
    });

  // ---- Dev container subcommands ----

  ampa
    .command('start-work')
    .description('Create an isolated dev container for a work item')
    .arguments('<work-item-id>')
    .option('--agent <name>', 'Agent name for work item assignment')
    .action(async (workItemId, opts) => {
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const code = await startWork(cwd, workItemId, opts.agent);
      process.exitCode = code;
    });

  ampa
    .command('sw')
    .description('Alias for start-work')
    .arguments('<work-item-id>')
    .option('--agent <name>', 'Agent name for work item assignment')
    .action(async (workItemId, opts) => {
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const code = await startWork(cwd, workItemId, opts.agent);
      process.exitCode = code;
    });

  ampa
    .command('finish-work')
    .description('Commit, push, and clean up a dev container')
    .option('--force', 'Destroy container even with uncommitted changes', false)
    .action(async (opts) => {
      const code = await finishWork(opts.force);
      process.exitCode = code;
    });

  ampa
    .command('fw')
    .description('Alias for finish-work')
    .option('--force', 'Destroy container even with uncommitted changes', false)
    .action(async (opts) => {
      const code = await finishWork(opts.force);
      process.exitCode = code;
    });

  ampa
    .command('list-containers')
    .description('List dev containers created by start-work')
    .option('--json', 'Output JSON')
    .action(async (opts) => {
      const code = listContainers(!!opts.json);
      process.exitCode = code;
    });

  ampa
    .command('lc')
    .description('Alias for list-containers')
    .option('--json', 'Output JSON')
    .action(async (opts) => {
      const code = listContainers(!!opts.json);
      process.exitCode = code;
    });
}

export {
  CONTAINER_IMAGE,
  CONTAINER_PREFIX,
  DAEMON_NOT_RUNNING_MESSAGE,
  branchName,
  checkBinary,
  checkContainerExists,
  checkPrerequisites,
  containerName,
  getGitOrigin,
  listContainers,
  resolveDaemonStore,
  start,
  startWork,
  status,
  stop,
  validateWorkItem,
};
