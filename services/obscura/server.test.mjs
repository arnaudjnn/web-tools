import test from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { createApp, validateInput } from './server.mjs';

async function fixture(t, options = {}) {
  const server = createApp({ apiKey: 'test-key', ...options });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => new Promise((resolve) => { server.closeAllConnections(); server.close(resolve); }));
  return (path, body, authenticated = true) => fetch(`http://127.0.0.1:${server.address().port}${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'content-type': 'application/json', ...(authenticated ? { authorization: 'Bearer test-key' } : {}) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
}

test('health exposes the form compatibility gate and readiness', async (t) => {
  const request = await fixture(t, { ready: () => false });
  const response = await request('/healthz', undefined, false);
  assert.equal(response.status, 503);
  assert.equal((await response.json()).form_submission_supported, false);
});

test('authentication is mandatory for operations', async (t) => {
  const request = await fixture(t, { run: () => assert.fail('must not run') });
  assert.equal((await request('/render', { url: 'https://example.com' }, false)).status, 401);
});

test('form submissions fail closed before opening a browser', async (t) => {
  const request = await fixture(t, { run: () => assert.fail('must not run') });
  const response = await request('/form-submit', { url: 'https://example.com' });
  assert.equal(response.status, 501);
  assert.equal((await response.json()).form_submissions, 0);
});

test('successful rendering preserves upstream status and content', async (t) => {
  const request = await fixture(t, { run: async (path, params) => {
    assert.equal(path, '/render');
    assert.equal(params.url, 'https://example.com/');
    return { engine: 'obscura', status: 403, html: '<h1>Denied</h1>' };
  } });
  const response = await request('/render', { url: 'https://example.com' });
  assert.equal(response.status, 200);
  assert.equal((await response.json()).status, 403);
});

test('rejects unsupported endpoints instead of falling back to another browser', async (t) => {
  const request = await fixture(t, { run: () => assert.fail('must not run') });
  assert.equal((await request('/screenshot', { url: 'https://example.com' })).status, 404);
});

test('errors do not leak credentials and are never retried', async (t) => {
  let calls = 0;
  const request = await fixture(t, { run: () => { calls++; throw new Error('secret-proxy-password'); } });
  const response = await request('/eval', { url: 'https://example.com', js: 'document.title' });
  assert.equal(response.status, 502);
  assert.doesNotMatch(await response.text(), /secret-proxy-password/);
  assert.equal(calls, 1);
});

test('limits request bodies', async (t) => {
  const request = await fixture(t, { run: () => assert.fail('must not run') });
  assert.equal((await request('/eval', { url: 'https://example.com', js: 'x'.repeat(70_000) })).status, 413);
});

test('one operation at a time; overload does not launch another browser', async (t) => {
  let release;
  let started;
  const entered = new Promise((resolve) => { started = resolve; });
  const request = await fixture(t, { run: async () => {
    started();
    return new Promise((resolve) => { release = () => resolve({ ok: true }); });
  } });
  const first = request('/render', { url: 'https://example.com' });
  await entered;
  assert.equal((await request('/render', { url: 'https://example.com' })).status, 429);
  release();
  assert.equal((await first).status, 200);
});

test('validates schemes, credentials, deadlines and scripts', () => {
  for (const body of [null, [], { url: 'file:///etc/passwd' }, { url: 'https://user:password@example.com' },
    { url: 'https://example.com', timeout_ms: 0 }, { url: 'https://example.com', timeout_ms: 180001 },
    { url: 'https://example.com', wait_ms: -1 }, { url: 'https://example.com', js: '' }]) {
    assert.throws(() => validateInput(body, '/eval'));
  }
});
