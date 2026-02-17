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

/**
 * Print pool container availability as part of status output.
 */
function printPoolStatus(projectRoot) {
  try {
    const available = listAvailablePool(projectRoot);
    const state = getPoolState(projectRoot);
    const claimed = Object.keys(state).length;
    const total = available.length + claimed;
    console.log(`Sandbox container pool: ${available.length} available, ${claimed} claimed (${total} total, target ${POOL_SIZE} available)`);
  } catch (e) {
    // Pool status is best-effort; don't fail status if pool helpers error
  }
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
    printPoolStatus(projectRoot);
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
    printPoolStatus(projectRoot);
    return 3;
  }
    if (isRunning(pid)) {
    // verify ownership before reporting running
    const owned = pidOwnedByProject(projectRoot, pid, lpath);
    if (owned) {
      console.log(`running pid=${pid} log=${lpath}`);
      printPoolStatus(projectRoot);
      return 0;
    } else {
      try { fs.unlinkSync(ppath); } catch (e) {}
      console.log('stopped (stale pid file removed)');
      printPoolStatus(projectRoot);
      return 3;
    }
  } else {
    try { fs.unlinkSync(ppath); } catch (e) {}
    const alt = findMostRecentLog(projectRoot) || lpath;
    try { printLogErrors(alt); } catch (e) {}
    console.log('stopped (stale pid file removed)');
    printPoolStatus(projectRoot);
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
const TEMPLATE_CONTAINER_NAME = 'ampa-template';
const POOL_PREFIX = 'ampa-pool-';
const POOL_SIZE = 3;

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
 * Ensure the template container exists and is initialized.
 * On first run, creates a Distrobox container from the image and enters it
 * once to trigger full host-integration init (slow, one-off).
 * On subsequent runs, returns immediately because the template already exists.
 * Returns { ok, message }.
 */
function ensureTemplate() {
  if (checkContainerExists(TEMPLATE_CONTAINER_NAME)) {
    return { ok: true, message: 'Template container already exists' };
  }

  console.log('');
  console.log('='.repeat(72));
  console.log('  FIRST-TIME SETUP: Creating template container.');
  console.log('  This is a one-off step that takes several minutes while');
  console.log('  Distrobox integrates the host environment. Subsequent');
  console.log('  start-work runs will be much faster.');
  console.log('='.repeat(72));
  console.log('');

  console.log(`Creating template container "${TEMPLATE_CONTAINER_NAME}"...`);
  const createResult = spawnSync('distrobox', [
    'create',
    '--name', TEMPLATE_CONTAINER_NAME,
    '--image', CONTAINER_IMAGE,
    '--yes',
    '--no-entry',
  ], { encoding: 'utf8', stdio: 'inherit' });
  if (createResult.status !== 0) {
    return { ok: false, message: `Failed to create template (exit code ${createResult.status})` };
  }

  // Enter the template once to trigger Distrobox's full init.
  // Use stdio: inherit so the user sees real-time progress output.
  console.log('Initializing template (this is the slow part)...');
  const initResult = spawnSync('distrobox', [
    'enter', TEMPLATE_CONTAINER_NAME, '--', 'true',
  ], { encoding: 'utf8', stdio: 'inherit' });
  if (initResult.status !== 0) {
    // Clean up the broken template
    spawnSync('distrobox', ['rm', '--force', TEMPLATE_CONTAINER_NAME], { stdio: 'pipe' });
    return { ok: false, message: `Template init failed (exit code ${initResult.status})` };
  }

  // Stop the template — distrobox enter leaves it running and
  // distrobox create --clone refuses to clone a running container.
  spawnSync('podman', ['stop', TEMPLATE_CONTAINER_NAME], { stdio: 'pipe' });

  console.log('Template container ready.');
  return { ok: true, message: 'Template created and initialized' };
}

// ---------------------------------------------------------------------------
// Container pool — pre-warmed containers for instant start-work
// ---------------------------------------------------------------------------

/**
 * Generate the name for pool container at the given index.
 */
function poolContainerName(index) {
  return `${POOL_PREFIX}${index}`;
}

/**
 * Path to the pool state JSON file.
 * Stores a mapping of pool container name -> { workItemId, branch, claimedAt }
 * for containers that have been claimed by start-work.
 */
function poolStatePath(projectRoot) {
  return path.join(projectRoot, '.worklog', 'ampa', 'pool-state.json');
}

/**
 * Read the pool state from disk. Returns an object keyed by container name.
 */
function getPoolState(projectRoot) {
  const p = poolStatePath(projectRoot);
  try {
    if (fs.existsSync(p)) {
      return JSON.parse(fs.readFileSync(p, 'utf8'));
    }
  } catch (e) {}
  return {};
}

/**
 * Persist pool state to disk.
 */
function savePoolState(projectRoot, state) {
  const p = poolStatePath(projectRoot);
  const dir = path.dirname(p);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(p, JSON.stringify(state, null, 2), 'utf8');
}

// Maximum pool index to scan.  This caps the total number of pool
// containers (claimed + unclaimed) to avoid runaway index growth.
const POOL_MAX_INDEX = POOL_SIZE * 3; // e.g. 9

/**
 * Return a Set of pool container names that currently exist in Podman.
 * Uses a single `podman ps -a` call instead of per-container checks.
 */
function existingPoolContainers() {
  const result = spawnSync('podman', [
    'ps', '-a', '--filter', `name=${POOL_PREFIX}`, '--format', '{{.Names}}',
  ], { encoding: 'utf8', stdio: 'pipe' });
  if (result.status !== 0) return new Set();
  return new Set(result.stdout.split('\n').filter(Boolean));
}

/**
 * List pool containers that exist in Podman and are NOT currently claimed.
 * Scans up to POOL_MAX_INDEX so we can find unclaimed containers even when
 * some lower-indexed slots are occupied by claimed (in-use) containers.
 * Returns an array of container names that are available for use.
 */
function listAvailablePool(projectRoot) {
  const state = getPoolState(projectRoot);
  const existing = existingPoolContainers();
  const available = [];
  for (let i = 0; i < POOL_MAX_INDEX; i++) {
    const name = poolContainerName(i);
    if (existing.has(name) && !state[name]) {
      available.push(name);
    }
  }
  return available;
}

/**
 * Claim a pool container for a work item. Updates the pool state file.
 * Returns the pool container name, or null if no pool containers are available.
 */
function claimPoolContainer(projectRoot, workItemId, branch) {
  const available = listAvailablePool(projectRoot);
  if (available.length === 0) return null;
  const name = available[0];
  const state = getPoolState(projectRoot);
  state[name] = {
    workItemId,
    branch,
    claimedAt: new Date().toISOString(),
  };
  savePoolState(projectRoot, state);
  return name;
}

/**
 * Release a pool container claim (after finish-work destroys it).
 */
function releasePoolContainer(projectRoot, containerNameOrAll) {
  const state = getPoolState(projectRoot);
  if (containerNameOrAll === '*') {
    // Clear all claims
    savePoolState(projectRoot, {});
    return;
  }
  delete state[containerNameOrAll];
  savePoolState(projectRoot, state);
}

/**
 * Look up which pool container is assigned to a work item ID.
 * Returns the pool container name or null.
 */
function findPoolContainerForWorkItem(projectRoot, workItemId) {
  const state = getPoolState(projectRoot);
  for (const [name, info] of Object.entries(state)) {
    if (info && info.workItemId === workItemId) return name;
  }
  return null;
}

/**
 * Synchronously fill the pool so that at least POOL_SIZE unclaimed
 * containers are available.  Scans up to POOL_MAX_INDEX to find free
 * slot indices (no existing container), creates new clones there, and
 * enters each one to trigger Distrobox init.
 *
 * Returns { created, errors } — the count of newly created containers
 * and an array of error messages for any that failed.
 */
function replenishPool(projectRoot) {
  const state = getPoolState(projectRoot);
  let created = 0;
  const errors = [];

  // Count how many unclaimed containers already exist
  const existing = existingPoolContainers();
  let unclaimed = 0;
  for (let i = 0; i < POOL_MAX_INDEX; i++) {
    const name = poolContainerName(i);
    if (existing.has(name) && !state[name]) {
      unclaimed++;
    }
  }

  const deficit = POOL_SIZE - unclaimed;
  if (deficit <= 0) {
    return { created: 0, errors: [] };
  }

  // Collect free slot indices (where no container exists at all)
  const freeSlots = [];
  for (let i = 0; i < POOL_MAX_INDEX && freeSlots.length < deficit; i++) {
    const name = poolContainerName(i);
    if (!existing.has(name)) {
      freeSlots.push(name);
    }
  }

  if (freeSlots.length === 0) {
    return { created: 0, errors: [`No free pool slots available (all ${POOL_MAX_INDEX} indices occupied)`] };
  }

  // Ensure template exists
  const tmpl = ensureTemplate();
  if (!tmpl.ok) {
    return { created: 0, errors: [`Template not available: ${tmpl.message}`] };
  }

  // Stop the template — clone requires it to be stopped
  spawnSync('podman', ['stop', TEMPLATE_CONTAINER_NAME], { stdio: 'pipe' });

  for (const name of freeSlots) {
    const result = spawnSync('distrobox', [
      'create',
      '--clone', TEMPLATE_CONTAINER_NAME,
      '--name', name,
      '--yes',
      '--no-entry',
    ], { encoding: 'utf8', stdio: 'pipe' });
    if (result.status !== 0) {
      const msg = (result.stderr || result.stdout || '').trim();
      errors.push(`Failed to create ${name}: ${msg}`);
      continue;
    }

    // Enter the container once to trigger Distrobox's full init.
    // Without this step the first distrobox-enter at claim time would
    // run init and the bash --login shell would source profile files
    // before Distrobox finishes writing them, leaving host binaries
    // (git, wl, etc.) off the PATH.
    const initResult = spawnSync('distrobox', [
      'enter', name, '--', 'true',
    ], { encoding: 'utf8', stdio: 'pipe' });
    if (initResult.status !== 0) {
      const msg = (initResult.stderr || initResult.stdout || '').trim();
      errors.push(`Failed to init ${name}: ${msg}`);
      // Clean up the broken container
      spawnSync('distrobox', ['rm', '--force', name], { stdio: 'pipe' });
      continue;
    }

    // Stop the container — it must not be running when start-work
    // enters it later (and also so future --clone operations work).
    spawnSync('podman', ['stop', name], { stdio: 'pipe' });

    created++;
  }

  return { created, errors };
}

/**
 * Spawn a detached background process that replenishes the pool.
 * Returns immediately — the replenish happens asynchronously.
 */
function replenishPoolBackground(projectRoot) {
  // Build an inline Node script that does the replenish.
  // We import the plugin and call replenishPool directly.
  const pluginPath = path.resolve(projectRoot, 'plugins', 'wl_ampa', 'ampa.mjs');
  // Fallback to installed copy if source of truth does not exist
  const actualPath = fs.existsSync(pluginPath)
    ? pluginPath
    : path.resolve(projectRoot, '.worklog', 'plugins', 'ampa.mjs');

  const script = [
    `import('file://${actualPath}')`,
    `.then(m => {`,
    `  const r = m.replenishPool('${projectRoot.replace(/'/g, "\\'")}');`,
    `  if (r.errors.length) r.errors.forEach(e => process.stderr.write(e + '\\n'));`,
    `})`,
    `.catch(e => { process.stderr.write(String(e) + '\\n'); process.exit(1); });`,
  ].join('');

  const logFile = path.join(projectRoot, '.worklog', 'ampa', 'pool-replenish.log');
  const out = fs.openSync(logFile, 'a');
  try {
    fs.appendFileSync(logFile, `\n--- replenish started at ${new Date().toISOString()} ---\n`);
  } catch (e) {}

  const child = spawn(process.execPath, ['--input-type=module', '-e', script], {
    cwd: projectRoot,
    detached: true,
    stdio: ['ignore', out, out],
  });
  child.unref();
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
  console.log(`Creating sandbox container to work on ${workItemId}...`);

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

  // 3. Check if this work item already has a claimed container
  const existingPool = findPoolContainerForWorkItem(projectRoot, workItemId);
  if (existingPool) {
    console.error(`Work item "${workItemId}" already has container "${existingPool}". Use 'wl ampa list-containers' to inspect or 'wl ampa finish-work' to clean up.`);
    return 2;
  }

  // Also check legacy container name (ampa-<id>) for backwards compat
  const legacyName = containerName(workItemId);
  if (checkContainerExists(legacyName)) {
    console.error(`Container "${legacyName}" already exists. Use 'wl ampa list-containers' to inspect or 'wl ampa finish-work' to clean up.`);
    return 2;
  }

  // 4. Get git origin
  const origin = getGitOrigin();
  if (!origin) {
    console.error('Could not determine git remote origin. Ensure this is a git repo with a remote named "origin".');
    return 2;
  }
  // Extract project name from origin URL (e.g. "SorraAgents" from
  // "git@github.com:Org/SorraAgents.git" or "https://…/SorraAgents.git")
  const projectName = origin.replace(/\.git$/, '').split('/').pop().split(':').pop();

  // 5. Build image if needed
  if (!imageExists(CONTAINER_IMAGE)) {
    const build = buildImage(projectRoot);
    if (!build.ok) {
      console.error(`Failed to build container image: ${build.message}`);
      return 2;
    }
  }

  // 6. Ensure template container exists (one-off slow init)
  const tmpl = ensureTemplate();
  if (!tmpl.ok) {
    console.error(`Failed to prepare template container: ${tmpl.message}`);
    return 1;
  }

  // 7. Derive branch name
  const branch = branchName(workItemId, workItem.issueType);

  // 8. Claim a pre-warmed pool container, or fall back to direct clone
  let cName = claimPoolContainer(projectRoot, workItemId, branch);
  if (cName) {
    console.log(`Using pre-warmed container "${cName}".`);
  } else {
     // Pool is empty — fall back to cloning from template directly
    console.log('No pre-warmed containers available, cloning from template...');
    spawnSync('podman', ['stop', TEMPLATE_CONTAINER_NAME], { stdio: 'pipe' });
    // Use the first pool slot name so it integrates with the pool system
    cName = poolContainerName(0);
    const createResult = runSync('distrobox', [
      'create',
      '--clone', TEMPLATE_CONTAINER_NAME,
      '--name', cName,
      '--yes',
      '--no-entry',
    ]);
    if (createResult.status !== 0) {
      console.error(`Failed to create container: ${createResult.stderr || createResult.stdout}`);
      return 1;
    }
    // Enter once to trigger Distrobox init (sets up host PATH integration)
    console.log('Initializing container...');
    const initResult = spawnSync('distrobox', [
      'enter', cName, '--', 'true',
    ], { encoding: 'utf8', stdio: 'inherit' });
    if (initResult.status !== 0) {
      console.error('Container init failed');
      spawnSync('distrobox', ['rm', '--force', cName], { stdio: 'pipe' });
      return 1;
    }
    spawnSync('podman', ['stop', cName], { stdio: 'pipe' });
    // Record the claim
    const state = getPoolState(projectRoot);
    state[cName] = {
      workItemId,
      branch,
      claimedAt: new Date().toISOString(),
    };
    savePoolState(projectRoot, state);
  }

  // 9. Run setup inside the container:
  //    - Clone the project (shallow)
  //    - Create/checkout branch
  //    - Set env vars for container detection
  //    - Run wl init + wl sync
  const setupScript = [
    `set -e`,
    // Symlink host Node.js into the container so tools like wl work.
    // Node bundles its own OpenSSL so it is safe to use from /run/host
    // (unlike git/ssh which must be installed natively).
    `if [ -x /run/host/usr/bin/node ] && [ ! -e /usr/local/bin/node ]; then`,
    `  sudo ln -s /run/host/usr/bin/node /usr/local/bin/node`,
    `fi`,
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
    `echo "export AMPA_PROJECT_ROOT=${projectRoot}" >> ~/.bashrc`,
    // Set a custom prompt so the user knows they are in a sandbox
    // Green for project_sandbox, cyan for branch, reset before newline/dollar
    `echo 'export PS1="\\[\\e[32m\\]${projectName}_sandbox\\[\\e[0m\\] - \\[\\e[36m\\]${branch}\\[\\e[0m\\]\\n\\$ "' >> ~/.bashrc`,
    `echo 'cd /workdir/project' >> ~/.bashrc`,
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
    'enter', cName, '--', 'bash', '--login', '-c', setupScript,
  ]);
  if (setupResult.status !== 0) {
    console.error(`Container setup failed: ${setupResult.stderr || setupResult.stdout}`);
    // Attempt cleanup
    releasePoolContainer(projectRoot, cName);
    spawnSync('distrobox', ['rm', '--force', cName], { stdio: 'pipe' });
    return 1;
  }
  if (setupResult.stdout) console.log(setupResult.stdout);

  // 10. Claim work item if agent name provided
  if (agentName) {
    spawnSync('wl', ['update', workItemId, '--status', 'in_progress', '--assignee', agentName, '--json'], {
      stdio: 'pipe',
      encoding: 'utf8',
    });
  }

  // 11. Replenish the pool in the background (replace the container we just used)
  replenishPoolBackground(projectRoot);

  // 12. Enter the container interactively
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
async function finishWork(force = false, workItemIdArg) {
  // 1. Detect context — running inside a container or from the host?
  const insideContainer = !!process.env.AMPA_CONTAINER_NAME;

  let cName, workItemId, branch, projectRoot;

  if (insideContainer) {
    // Inside-container path: read env vars set by start-work
    cName = process.env.AMPA_CONTAINER_NAME;
    workItemId = process.env.AMPA_WORK_ITEM_ID;
    branch = process.env.AMPA_BRANCH;
    projectRoot = process.env.AMPA_PROJECT_ROOT;
  } else {
    // Host path: look up the container from pool state
    projectRoot = process.cwd();
    try { projectRoot = findProjectRoot(projectRoot); } catch (e) {
      console.error(e.message);
      return 2;
    }

    const state = getPoolState(projectRoot);
    const claimed = Object.entries(state).filter(([, v]) => v.workItemId);

    if (claimed.length === 0) {
      console.error('No claimed containers found. Nothing to finish.');
      return 2;
    }

    if (workItemIdArg) {
      // Find the container for the given work item
      const match = claimed.find(([, v]) => v.workItemId === workItemIdArg);
      if (!match) {
        console.error(`No container found for work item "${workItemIdArg}".`);
        console.error('Claimed containers:');
        for (const [name, v] of claimed) {
          console.error(`  ${name} → ${v.workItemId} (${v.branch})`);
        }
        return 2;
      }
      [cName, { workItemId, branch }] = [match[0], match[1]];
    } else if (claimed.length === 1) {
      // Only one claimed container — use it automatically
      [cName, { workItemId, branch }] = [claimed[0][0], claimed[0][1]];
      console.log(`Using container "${cName}" (${workItemId})`);
    } else {
      // Multiple claimed containers — require explicit ID
      console.error('Multiple claimed containers found. Specify a work item ID:');
      for (const [name, v] of claimed) {
        console.error(`  wl ampa finish-work ${v.workItemId}  (container: ${name}, branch: ${v.branch})`);
      }
      return 2;
    }
  }

  if (!cName || !workItemId) {
    console.error('Could not determine container or work item. Use "wl ampa finish-work <work-item-id>" from the host or run from inside a container.');
    return 2;
  }

  if (insideContainer) {
    // --- Inside-container path: commit, push, mark for cleanup ---

    // 2. Check for uncommitted changes
    const statusResult = runSync('git', ['status', '--porcelain']);
    const hasUncommitted = statusResult.stdout.length > 0;

    if (hasUncommitted && !force) {
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

    // 5. Release pool claim and mark for cleanup
    if (projectRoot) {
      try {
        releasePoolContainer(projectRoot, cName);
      } catch (e) {
        // Non-fatal — pool state file may not be accessible from inside container
      }
    }

    console.log(`Container "${cName}" marked for cleanup.`);
    console.log('Run the following from the host to destroy the container:');
    console.log(`  distrobox rm --force ${cName}`);

    return 0;
  }

  // --- Host path: enter container, commit/push, destroy, replenish ---

  console.log(`Finishing work in container "${cName}" (${workItemId}, branch: ${branch})...`);

  if (!force) {
    // Build a script to commit and push inside the container
    const commitPushScript = [
      `set -e`,
      `cd /workdir/project 2>/dev/null || { echo "No project directory found in container."; exit 1; }`,
      // Check for uncommitted changes
      `if [ -n "$(git status --porcelain)" ]; then`,
      `  echo "Uncommitted changes detected. Committing..."`,
      `  git add -A`,
      `  git commit -m "${workItemId}: Work completed in dev container"`,
      `fi`,
      // Push
      `PUSH_BRANCH="${branch || 'HEAD'}"`,
      `echo "Pushing $PUSH_BRANCH to origin..."`,
      `git push -u origin "$PUSH_BRANCH"`,
      `echo "AMPA_COMMIT_HASH=$(git rev-parse --short HEAD)"`,
    ].join('\n');

    console.log('Entering container to commit and push...');
    const commitResult = runSync('distrobox', [
      'enter', cName, '--', 'bash', '--login', '-c', commitPushScript,
    ]);

    if (commitResult.status !== 0) {
      console.error(`Commit/push inside container failed: ${commitResult.stderr || commitResult.stdout}`);
      console.error('Use --force to destroy the container without committing (changes will be lost).');
      return 1;
    }
    if (commitResult.stdout) console.log(commitResult.stdout);

    // Extract commit hash from output
    const hashMatch = (commitResult.stdout || '').match(/AMPA_COMMIT_HASH=(\S+)/);
    const commitHash = hashMatch ? hashMatch[1] : 'unknown';

    // Update work item from the host
    console.log(`Updating work item ${workItemId}...`);
    spawnSync('wl', ['update', workItemId, '--stage', 'in_review', '--status', 'completed', '--json'], {
      stdio: 'pipe',
      encoding: 'utf8',
    });
    spawnSync('wl', ['comment', 'add', workItemId, '--comment', `Work completed in dev container ${cName}. Branch: ${branch || 'HEAD'}. Latest commit: ${commitHash}`, '--author', 'ampa', '--json'], {
      stdio: 'pipe',
      encoding: 'utf8',
    });
  } else {
    console.log('Warning: Skipping commit/push (--force). Uncommitted changes will be lost.');
  }

  // Release pool claim
  try {
    releasePoolContainer(projectRoot, cName);
    console.log(`Released pool claim for "${cName}".`);
  } catch (e) {
    console.error(`Warning: Could not release pool claim: ${e.message}`);
  }

  // Destroy the container
  console.log(`Destroying container "${cName}"...`);
  const rmResult = runSync('distrobox', ['rm', '--force', cName]);
  if (rmResult.status !== 0) {
    console.error(`Warning: Container removal failed: ${rmResult.stderr || rmResult.stdout}`);
    console.error(`You may need to run: distrobox rm --force ${cName}`);
  } else {
    console.log(`Container "${cName}" destroyed.`);
  }

  // Replenish pool in background
  replenishPoolBackground(projectRoot);
  console.log('Pool replenishment started in background.');

  return 0;
}

/**
 * List all dev containers created by start-work.
 * Shows claimed pool containers with their work item mapping.
 * Hides unclaimed pool containers and the template container.
 */
function listContainers(projectRoot, useJson = false) {
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

  const poolState = getPoolState(projectRoot);

  const lines = result.stdout.split('\n').filter(Boolean);
  const containers = lines.map((line) => {
    const parts = line.split('\t');
    const name = parts[0] || '';
    const status = parts[1] || 'unknown';
    const created = parts[2] || 'unknown';

    // Check if this is a pool container with a work item claim
    const claim = poolState[name];
    if (claim) {
      return { name, workItemId: claim.workItemId, branch: claim.branch, status, created };
    }

    // Legacy container name: ampa-<work-item-id> (not pool, not template)
    if (name.startsWith(CONTAINER_PREFIX) && !name.startsWith(POOL_PREFIX) && name !== TEMPLATE_CONTAINER_NAME) {
      const workItemId = name.slice(CONTAINER_PREFIX.length);
      return { name, workItemId, status, created };
    }

    // Unclaimed pool container or template — mark for filtering
    return null;
  }).filter(Boolean);

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
    .arguments('[work-item-id]')
    .option('--force', 'Destroy container even with uncommitted changes', false)
    .action(async (workItemId, opts) => {
      const code = await finishWork(opts.force, workItemId);
      process.exitCode = code;
    });

  ampa
    .command('fw')
    .description('Alias for finish-work')
    .arguments('[work-item-id]')
    .option('--force', 'Destroy container even with uncommitted changes', false)
    .action(async (workItemId, opts) => {
      const code = await finishWork(opts.force, workItemId);
      process.exitCode = code;
    });

  ampa
    .command('list-containers')
    .description('List dev containers created by start-work')
    .option('--json', 'Output JSON')
    .action(async (opts) => {
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const code = listContainers(cwd, !!opts.json);
      process.exitCode = code;
    });

  ampa
    .command('lc')
    .description('Alias for list-containers')
    .option('--json', 'Output JSON')
    .action(async (opts) => {
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const code = listContainers(cwd, !!opts.json);
      process.exitCode = code;
    });

  ampa
    .command('warm-pool')
    .description('Pre-warm the container pool (ensure template exists and fill empty pool slots)')
    .action(async () => {
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const prereqs = checkPrerequisites();
      if (!prereqs.ok) {
        console.error(prereqs.message);
        process.exitCode = 1;
        return;
      }
      console.log('Ensuring template container exists...');
      const tmpl = ensureTemplate();
      if (!tmpl.ok) {
        console.error(`Failed to create template: ${tmpl.message}`);
        process.exitCode = 1;
        return;
      }
      console.log('Template ready. Filling pool slots...');
      const result = replenishPool(cwd);
      if (result.errors.length) {
        result.errors.forEach(e => console.error(e));
      }
      if (result.created > 0) {
        console.log(`Created ${result.created} pool container(s). Pool is now warm.`);
      } else {
        console.log('Pool is already fully warm — no new containers needed.');
      }
      process.exitCode = result.errors.length > 0 ? 1 : 0;
    });

  ampa
    .command('wp')
    .description('Alias for warm-pool')
    .action(async () => {
      let cwd = process.cwd();
      try { cwd = findProjectRoot(cwd); } catch (e) { console.error(e.message); process.exitCode = 2; return; }
      const prereqs = checkPrerequisites();
      if (!prereqs.ok) {
        console.error(prereqs.message);
        process.exitCode = 1;
        return;
      }
      console.log('Ensuring template container exists...');
      const tmpl = ensureTemplate();
      if (!tmpl.ok) {
        console.error(`Failed to create template: ${tmpl.message}`);
        process.exitCode = 1;
        return;
      }
      console.log('Template ready. Filling pool slots...');
      const result = replenishPool(cwd);
      if (result.errors.length) {
        result.errors.forEach(e => console.error(e));
      }
      if (result.created > 0) {
        console.log(`Created ${result.created} pool container(s). Pool is now warm.`);
      } else {
        console.log('Pool is already fully warm — no new containers needed.');
      }
      process.exitCode = result.errors.length > 0 ? 1 : 0;
    });
}

export {
  CONTAINER_IMAGE,
  CONTAINER_PREFIX,
  DAEMON_NOT_RUNNING_MESSAGE,
  POOL_PREFIX,
  POOL_SIZE,
  POOL_MAX_INDEX,
  TEMPLATE_CONTAINER_NAME,
  branchName,
  checkBinary,
  checkContainerExists,
  checkPrerequisites,
  claimPoolContainer,
  containerName,
  ensureTemplate,
  existingPoolContainers,
  findPoolContainerForWorkItem,
  getGitOrigin,
  getPoolState,
  listAvailablePool,
  listContainers,
  poolContainerName,
  poolStatePath,
  releasePoolContainer,
  replenishPool,
  replenishPoolBackground,
  resolveDaemonStore,
  savePoolState,
  start,
  startWork,
  status,
  stop,
  validateWorkItem,
};
