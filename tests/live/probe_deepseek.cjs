// Trusted native-library probe, not a model task or an Agent Center sandbox implementation.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const net = require('node:net');
const base = '/Applications/DeepSeek Harness.app/Contents/Resources/app.asar/dsh/node_modules/';
const { LocalSandboxProvider } = require(base + '@deepseek-ai/dsh-sandbox-local/lib/index.js');
const { Context } = require(base + '@deepseek-ai/cordis/lib/index.js');

(async () => {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.homedir(), '.agent-center-dsh-probe-')));
  const workspace = path.join(root, 'workspace');
  fs.mkdirSync(workspace);
  const sentinel = path.join(root, 'outside');
  fs.writeFileSync(sentinel, 'NONSECRET-SENTINEL');
  const server = net.createServer(socket => socket.end());
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const python = process.argv[2];
  if (!python || !path.isAbsolute(python)) throw Error('Explicit development Python path required');
  const provider = new LocalSandboxProvider(new Context(), {
    runnerCommand: [], runnerFailureSignatures: [], probeTimeoutMs: 5000,
  });
  const records = [];
  try {
    for (const mode of ['read-only', 'workspace-write']) {
      const probes = {
        workspace_read: "from pathlib import Path; print(Path('.').resolve())",
        workspace_write: "from pathlib import Path; Path('probe').write_text('ok')",
        outside_write: `from pathlib import Path; Path(${JSON.stringify(sentinel)}).write_text('changed')`,
        outside_read: `from pathlib import Path; print(Path(${JSON.stringify(sentinel)}).read_text())`,
        loopback_network: `import socket; s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1',${port})); print('connected')`,
      };
      for (const [name, source] of Object.entries(probes)) {
        try {
          const confined = await provider.confine([python, '-B', '-c', source], { mode, workspaceRoot: workspace });
          const result = spawnSync(confined.argv[0], confined.argv.slice(1), {
            cwd: workspace, env: { PATH: '/usr/bin:/bin' }, encoding: 'utf8', timeout: 10000,
          });
          records.push({ mode, probe: name, enforcement: confined.enforcement,
            returncode: result.status, signal: result.signal, stdout: result.stdout.trim(), stderr: result.stderr.trim() });
        } catch (error) {
          records.push({ mode, probe: name, error: String(error) });
        }
        if (fs.existsSync(path.join(workspace, 'probe'))) fs.unlinkSync(path.join(workspace, 'probe'));
      }
    }
    records.push({ outside_sentinel_unchanged: fs.readFileSync(sentinel, 'utf8') === 'NONSECRET-SENTINEL' });
    console.log(JSON.stringify(records, null, 2));
  } finally {
    server.close();
    fs.rmSync(root, { recursive: true, force: true });
  }
})().catch(error => { console.error(String(error)); process.exitCode = 1; });
