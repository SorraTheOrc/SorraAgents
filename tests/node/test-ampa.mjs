import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'child_process';
import fs from 'fs';
import path from 'path';
import os from 'os';

// Lightweight lifecycle test for the Node ampa plugin when installed into
// .worklog/plugins in a temporary project directory.

const pluginModule = new URL('../../skill/install-ampa/resources/ampa.mjs', import.meta.url);
const plugin = await import(pluginModule.href);

class FakeProgram {
  constructor() {
    this.commands = new Map();
  }

  command(name) {
    const cmd = new FakeCommand(name, this);
    this.commands.set(name, cmd);
    return cmd;
  }
}

class FakeCommand {
  constructor(name, program) {
    this.name = name;
    this.program = program;
    this.subcommands = new Map();
    this.actionFn = null;
  }

  command(name) {
    const cmd = new FakeCommand(name, this.program);
    this.subcommands.set(name, cmd);
    return cmd;
  }

  description() {
    return this;
  }

  option() {
    return this;
  }

  arguments() {
    return this;
  }

  action(fn) {
    this.actionFn = fn;
    return this;
  }
}

async function withTempDir(name, fn) {
  const tmp = path.join(process.cwd(), name);
  if (!fs.existsSync(tmp)) fs.mkdirSync(tmp);
  try {
    return await fn(tmp);
  } finally {
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (e) {}
  }
}

test('ampa list requires running daemon', async (t) => {
  await withTempDir('tmp-ampa-list-test', async (tmp) => {
    fs.mkdirSync(path.join(tmp, '.worklog', 'ampa', 't1'), { recursive: true });

    const state = plugin.resolveDaemonStore(tmp, 't1');
    assert.equal(state.running, false, 'daemon should be reported as not running');
    assert.equal(
      plugin.DAEMON_NOT_RUNNING_MESSAGE,
      'Daemon is not running. Start it with: wl ampa start'
    );
  });
});

test('ampa start/status/stop lifecycle', async (t) => {
  await withTempDir('tmp-ampa-test', async (tmp) => {
    const daemon = path.join(tmp, 'test_daemon.js');
    fs.writeFileSync(
      daemon,
      `process.on('SIGTERM', ()=>{ console.log('got TERM'); process.exit(0); }); setInterval(()=>{},1000);`
    );
    fs.chmodSync(daemon, 0o755);
    fs.writeFileSync(path.join(tmp, 'worklog.json'), JSON.stringify({ ampa: `node ${daemon}` }));

    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampaCmd = ctx.program.commands.get('ampa');
    const startCmd = ampaCmd.subcommands.get('start');
    const statusCmd = ampaCmd.subcommands.get('status');
    const stopCmd = ampaCmd.subcommands.get('stop');

    await startCmd.actionFn({ name: 't1', cmd: null, foreground: false, verbose: false });

    let output = '';
    const originalLog = console.log;
    console.log = (...args) => {
      output += args.join(' ') + '\n';
    };
    await statusCmd.actionFn({ name: 't1' });
    console.log = originalLog;
    assert.ok(/running pid=\d+/.test(output), `status output unexpected: ${output}`);

    await stopCmd.actionFn({ name: 't1' });
  });
});

test('ampa list resolves daemon store env', async (t) => {
  await withTempDir('tmp-ampa-store-test', async (tmp) => {
    const daemon = path.join(tmp, 'daemon.js');
    fs.writeFileSync(daemon, 'setInterval(()=>{},1000);');
    fs.chmodSync(daemon, 0o755);

    const storeRel = 'stores/active.json';
    const proc = spawn('node', [daemon], {
      cwd: tmp,
      env: Object.assign({}, process.env, { AMPA_SCHEDULER_STORE: storeRel }),
      stdio: 'ignore',
      detached: true,
    });
    assert.ok(proc.pid, 'expected daemon pid');
    await new Promise((resolve) => setTimeout(resolve, 50));

    const base = path.join(tmp, '.worklog', 'ampa', 't1');
    fs.mkdirSync(base, { recursive: true });
    fs.writeFileSync(path.join(base, 't1.pid'), String(proc.pid));

    const state = plugin.resolveDaemonStore(tmp, 't1');
    assert.equal(state.running, true, 'daemon should be reported running');
    assert.equal(state.storePath, path.resolve(tmp, storeRel));

    try {
      process.kill(-proc.pid, 'SIGTERM');
    } catch (e) {
      try { process.kill(proc.pid, 'SIGTERM'); } catch (e2) {}
    }
    proc.unref();
  });
});

test('ampa list uses bundled ampa store when no env override', async (t) => {
  await withTempDir('tmp-ampa-store-bundle-test', async (tmp) => {
    const daemon = path.join(tmp, 'daemon.js');
    fs.writeFileSync(daemon, 'setInterval(()=>{},1000);');
    fs.chmodSync(daemon, 0o755);
    const proc = spawn('node', [daemon], {
      cwd: tmp,
      env: Object.assign({}, process.env),
      stdio: 'ignore',
      detached: true,
    });
    assert.ok(proc.pid, 'expected daemon pid');
    await new Promise((resolve) => setTimeout(resolve, 50));

    const base = path.join(tmp, '.worklog', 'ampa', 't1');
    fs.mkdirSync(base, { recursive: true });
    fs.writeFileSync(path.join(base, 't1.pid'), String(proc.pid));

    const bundlePath = path.join(tmp, '.worklog', 'plugins', 'ampa_py', 'ampa');
    fs.mkdirSync(bundlePath, { recursive: true });
    fs.writeFileSync(path.join(bundlePath, 'scheduler.py'), '# placeholder');

    const state = plugin.resolveDaemonStore(tmp, 't1');
    assert.equal(state.running, true, 'daemon should be reported running');
    assert.equal(state.storePath, path.join(bundlePath, 'scheduler_store.json'));

    try {
      process.kill(-proc.pid, 'SIGTERM');
    } catch (e) {
      try { process.kill(proc.pid, 'SIGTERM'); } catch (e2) {}
    }
    proc.unref();
  });
});

test('ampa list verbose prints store path', async (t) => {
  await withTempDir('tmp-ampa-verbose-test', async (tmp) => {
    const ampaDir = path.join(tmp, 'ampa');
    fs.mkdirSync(ampaDir, { recursive: true });
    const daemon = path.join(ampaDir, 'daemon.js');
    fs.writeFileSync(daemon, 'setInterval(()=>{},1000);');
    fs.chmodSync(daemon, 0o755);

    const storeRel = 'stores/active.json';
    const storePath = path.resolve(tmp, storeRel);
    const proc = spawn('node', [daemon], {
      cwd: tmp,
      env: Object.assign({}, process.env, { AMPA_SCHEDULER_STORE: storeRel }),
      stdio: 'ignore',
      detached: true,
    });
    assert.ok(proc.pid, 'expected daemon pid');
    await new Promise((resolve) => setTimeout(resolve, 50));

    const base = path.join(tmp, '.worklog', 'ampa', 't1');
    fs.mkdirSync(base, { recursive: true });
    fs.writeFileSync(path.join(base, 't1.pid'), String(proc.pid));

    fs.writeFileSync(path.join(ampaDir, '__init__.py'), '');
    fs.writeFileSync(
      path.join(ampaDir, 'scheduler.py'),
      'import sys\n\nif __name__ == "__main__":\n    if "list" in sys.argv:\n        print("[]")\n'
    );

    const ctx = { program: new FakeProgram() };
    plugin.default(ctx);
    const ampaCmd = ctx.program.commands.get('ampa');
    const listCmd = ampaCmd.subcommands.get('list');

    let output = '';
    const originalLog = console.log;
    console.log = (...args) => {
      output += args.join(' ') + '\n';
    };
    const originalCwd = process.cwd();
    try {
      process.chdir(tmp);
      await listCmd.actionFn({ name: 't1', json: true, verbose: true });
    } finally {
      process.chdir(originalCwd);
      console.log = originalLog;
    }

    assert.ok(output.includes(`Using scheduler store: ${storePath}`), `verbose output missing store: ${output}`);

    try {
      process.kill(-proc.pid, 'SIGTERM');
    } catch (e) {
      try { process.kill(proc.pid, 'SIGTERM'); } catch (e2) {}
    }
    proc.unref();

  });
});

// ---------- resolveAmpaPackage tests ----------

test('resolveAmpaPackage finds local package first', async () => {
  await withTempDir('tmp-ampa-pkg-local', async (tmp) => {
    // Create local package
    const localPy = path.join(tmp, '.worklog', 'plugins', 'ampa_py', 'ampa');
    fs.mkdirSync(localPy, { recursive: true });
    fs.writeFileSync(path.join(localPy, '__init__.py'), '');

    const result = plugin.resolveAmpaPackage(tmp);
    assert.ok(result, 'should find local package');
    assert.equal(result.pyPath, path.join(tmp, '.worklog', 'plugins', 'ampa_py'));
    assert.equal(result.pythonBin, 'python3'); // no venv
  });
});

test('resolveAmpaPackage finds global package when local absent', async () => {
  const savedXdg = process.env.XDG_CONFIG_HOME;
  const xdgDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pkg-global-'));
  try {
    process.env.XDG_CONFIG_HOME = xdgDir;

    await withTempDir('tmp-ampa-pkg-global-proj', async (tmp) => {
      // No local package — create global package
      const globalPy = path.join(xdgDir, 'opencode', '.worklog', 'plugins', 'ampa_py', 'ampa');
      fs.mkdirSync(globalPy, { recursive: true });
      fs.writeFileSync(path.join(globalPy, '__init__.py'), '');

      const result = plugin.resolveAmpaPackage(tmp);
      assert.ok(result, 'should find global package');
      assert.equal(result.pyPath, path.join(xdgDir, 'opencode', '.worklog', 'plugins', 'ampa_py'));
    });
  } finally {
    if (savedXdg === undefined) delete process.env.XDG_CONFIG_HOME;
    else process.env.XDG_CONFIG_HOME = savedXdg;
    try { fs.rmSync(xdgDir, { recursive: true, force: true }); } catch (e) {}
  }
});

test('resolveAmpaPackage prefers local over global', async () => {
  const savedXdg = process.env.XDG_CONFIG_HOME;
  const xdgDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pkg-pref-'));
  try {
    process.env.XDG_CONFIG_HOME = xdgDir;

    await withTempDir('tmp-ampa-pkg-prefer', async (tmp) => {
      // Create both local and global packages
      const localPy = path.join(tmp, '.worklog', 'plugins', 'ampa_py', 'ampa');
      fs.mkdirSync(localPy, { recursive: true });
      fs.writeFileSync(path.join(localPy, '__init__.py'), '');

      const globalPy = path.join(xdgDir, 'opencode', '.worklog', 'plugins', 'ampa_py', 'ampa');
      fs.mkdirSync(globalPy, { recursive: true });
      fs.writeFileSync(path.join(globalPy, '__init__.py'), '');

      const result = plugin.resolveAmpaPackage(tmp);
      assert.ok(result, 'should find a package');
      assert.equal(result.pyPath, path.join(tmp, '.worklog', 'plugins', 'ampa_py'),
        'should prefer local over global');
    });
  } finally {
    if (savedXdg === undefined) delete process.env.XDG_CONFIG_HOME;
    else process.env.XDG_CONFIG_HOME = savedXdg;
    try { fs.rmSync(xdgDir, { recursive: true, force: true }); } catch (e) {}
  }
});

test('resolveAmpaPackage returns null when no package exists', async () => {
  const savedXdg = process.env.XDG_CONFIG_HOME;
  const xdgDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-pkg-none-'));
  try {
    process.env.XDG_CONFIG_HOME = xdgDir;

    await withTempDir('tmp-ampa-pkg-none', async (tmp) => {
      const result = plugin.resolveAmpaPackage(tmp);
      assert.equal(result, null, 'should return null when no package found');
    });
  } finally {
    if (savedXdg === undefined) delete process.env.XDG_CONFIG_HOME;
    else process.env.XDG_CONFIG_HOME = savedXdg;
    try { fs.rmSync(xdgDir, { recursive: true, force: true }); } catch (e) {}
  }
});

// ---------- projectAmpaDir tests ----------

test('projectAmpaDir returns correct path', () => {
  assert.equal(plugin.projectAmpaDir('/foo/bar'), path.join('/foo/bar', '.worklog', 'ampa'));
});

// ---------- globalPluginsDir tests ----------

test('globalPluginsDir respects XDG_CONFIG_HOME', () => {
  const saved = process.env.XDG_CONFIG_HOME;
  try {
    process.env.XDG_CONFIG_HOME = '/tmp/test-xdg';
    assert.equal(plugin.globalPluginsDir(), path.join('/tmp/test-xdg', 'opencode', '.worklog', 'plugins'));
  } finally {
    if (saved === undefined) delete process.env.XDG_CONFIG_HOME;
    else process.env.XDG_CONFIG_HOME = saved;
  }
});

// ---------- resolveDaemonStore fallback tests ----------

test('resolveDaemonStore falls back to projectAmpaDir when no package found', async () => {
  const savedXdg = process.env.XDG_CONFIG_HOME;
  const xdgDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-store-fb-'));
  try {
    process.env.XDG_CONFIG_HOME = xdgDir;

    await withTempDir('tmp-ampa-store-fallback', async (tmp) => {
      const daemon = path.join(tmp, 'daemon.js');
      fs.writeFileSync(daemon, 'setInterval(()=>{},1000);');
      fs.chmodSync(daemon, 0o755);
      const proc = spawn('node', [daemon], {
        cwd: tmp,
        env: Object.assign({}, process.env, { XDG_CONFIG_HOME: xdgDir }),
        stdio: 'ignore',
        detached: true,
      });
      assert.ok(proc.pid, 'expected daemon pid');
      await new Promise((resolve) => setTimeout(resolve, 50));

      const base = path.join(tmp, '.worklog', 'ampa', 't1');
      fs.mkdirSync(base, { recursive: true });
      fs.writeFileSync(path.join(base, 't1.pid'), String(proc.pid));

      // No ampa_py package anywhere — should fall back to projectAmpaDir
      const state = plugin.resolveDaemonStore(tmp, 't1');
      assert.equal(state.running, true, 'daemon should be running');
      assert.equal(state.storePath, path.join(tmp, '.worklog', 'ampa', 'scheduler_store.json'),
        'should fall back to projectAmpaDir/scheduler_store.json');

      try {
        process.kill(-proc.pid, 'SIGTERM');
      } catch (e) {
        try { process.kill(proc.pid, 'SIGTERM'); } catch (e2) {}
      }
      proc.unref();
    });
  } finally {
    if (savedXdg === undefined) delete process.env.XDG_CONFIG_HOME;
    else process.env.XDG_CONFIG_HOME = savedXdg;
    try { fs.rmSync(xdgDir, { recursive: true, force: true }); } catch (e) {}
  }
});

test('resolveDaemonStore finds global ampa_py package', async () => {
  const savedXdg = process.env.XDG_CONFIG_HOME;
  const xdgDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ampa-store-gbl-'));
  try {
    process.env.XDG_CONFIG_HOME = xdgDir;

    await withTempDir('tmp-ampa-store-global', async (tmp) => {
      const daemon = path.join(tmp, 'daemon.js');
      fs.writeFileSync(daemon, 'setInterval(()=>{},1000);');
      fs.chmodSync(daemon, 0o755);
      const proc = spawn('node', [daemon], {
        cwd: tmp,
        env: Object.assign({}, process.env, { XDG_CONFIG_HOME: xdgDir }),
        stdio: 'ignore',
        detached: true,
      });
      assert.ok(proc.pid, 'expected daemon pid');
      await new Promise((resolve) => setTimeout(resolve, 50));

      const base = path.join(tmp, '.worklog', 'ampa', 't1');
      fs.mkdirSync(base, { recursive: true });
      fs.writeFileSync(path.join(base, 't1.pid'), String(proc.pid));

      // Create global ampa_py package with scheduler.py
      const globalPy = path.join(xdgDir, 'opencode', '.worklog', 'plugins', 'ampa_py', 'ampa');
      fs.mkdirSync(globalPy, { recursive: true });
      fs.writeFileSync(path.join(globalPy, 'scheduler.py'), '# placeholder');

      const state = plugin.resolveDaemonStore(tmp, 't1');
      assert.equal(state.running, true, 'daemon should be running');
      assert.equal(state.storePath, path.join(globalPy, 'scheduler_store.json'),
        'should find scheduler_store.json via global ampa_py package');

      try {
        process.kill(-proc.pid, 'SIGTERM');
      } catch (e) {
        try { process.kill(proc.pid, 'SIGTERM'); } catch (e2) {}
      }
      proc.unref();
    });
  } finally {
    if (savedXdg === undefined) delete process.env.XDG_CONFIG_HOME;
    else process.env.XDG_CONFIG_HOME = savedXdg;
    try { fs.rmSync(xdgDir, { recursive: true, force: true }); } catch (e) {}
  }
});