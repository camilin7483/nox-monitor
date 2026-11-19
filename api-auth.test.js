'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

function run(t, config, script = "require('./api.js')", flags = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'nox-auth-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  if (config !== undefined) fs.writeFileSync(path.join(dir, 'nox-config.json'), JSON.stringify(config));
  const env = { ...process.env, NOX_DATA_DIR: dir, NOX_PACKAGED: '0', NOX_API_TOKEN: '', ...flags };
  const invoke = () => spawnSync(process.execPath, ['-e', script], { cwd: __dirname, env, encoding: 'utf8', timeout: 10000 });
  return { dir, invoke };
}

test('development creates a persistent random private token instead of a legacy default', t => {
  const first = run(t, { api_token: 'nox-token' });
  const second = run(t);
  for (const instance of [first, second]) {
    const result = instance.invoke();
    assert.equal(result.status, 0, result.stderr);
  }
  const tokenFile = path.join(first.dir, '.nox-api-token');
  const token = fs.readFileSync(tokenFile, 'utf8');
  assert.match(token, /^[A-Za-z0-9_-]{43}$/);
  assert.notEqual(token, fs.readFileSync(path.join(second.dir, '.nox-api-token'), 'utf8'));
  assert.equal(first.invoke().status, 0);
  assert.equal(fs.readFileSync(tokenFile, 'utf8'), token);
  if (process.platform !== 'win32') assert.equal(fs.statSync(tokenFile).mode & 0o077, 0);
});

test('explicit valid credentials do not create a fallback token', t => {
  const instance = run(t, { api_token: 'x'.repeat(43) });
  assert.equal(instance.invoke().status, 0);
  assert.equal(fs.existsSync(path.join(instance.dir, '.nox-api-token')), false);
});

test('packaged API rejects weak credentials and accepts only its configured token', t => {
  const invalid = run(t, { api_token: 'nox-token' }, undefined, { NOX_PACKAGED: '1' });
  assert.notEqual(invalid.invoke().status, 0);
  const token = 'a'.repeat(43);
  const script = `
    const assert = require('node:assert/strict');
    const { once } = require('node:events');
    (async () => {
      const server = require('./api.js').startServer({ db: {} });
      try {
        await once(server, 'listening');
        const url = 'http://127.0.0.1:' + server.address().port + '/api/stats';
        assert.equal((await fetch(url)).status, 401);
        assert.equal((await fetch(url, { headers: { Authorization: 'Bearer nox-token' } })).status, 401);
        assert.equal((await fetch(url, { headers: { Authorization: 'Bearer ' + 'a'.repeat(43) } })).status, 200);
      } finally { server.closeAllConnections(); server.close(); }
    })().catch(error => { console.error(error); process.exitCode = 1; });
  `;
  const instance = run(t, { api_token: token }, script, { NOX_PACKAGED: '1' });
  const result = instance.invoke();
  assert.equal(result.status, 0, result.stderr || String(result.error || ''));
});
