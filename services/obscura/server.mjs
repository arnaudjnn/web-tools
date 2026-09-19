// Isolated Obscura evaluation service. No stealth flags, retry chain or browser
// fallback. A fresh engine per operation isolates pages and bounds cleanup.
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { timingSafeEqual } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { Cdp } from './cdp.mjs';

export const VERSION = '0.2.2';
export const FORM_BLOCK_REASON = 'Obscura 0.2.2 reports native form POSTs as GET in CDP; reliable submission accounting is unavailable. No form was submitted.';
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function validateInput(body, operation) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('A JSON object is required');
  const url = new URL(body.url);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('An HTTP(S) URL without embedded credentials is required');
  const timeout = body.timeout_ms ?? 30_000;
  if (!Number.isInteger(timeout) || timeout < 1000 || timeout > 180_000) throw new Error('timeout_ms must be between 1000 and 180000');
  const wait = body.wait_ms ?? 0;
  if (!Number.isInteger(wait) || wait < 0 || wait > 30_000) throw new Error('wait_ms must be between 0 and 30000');
  if (operation === '/eval' && (typeof body.js !== 'string' || !body.js || body.js.length > 32_000)) throw new Error('js must contain between 1 and 32000 characters');
  return { url: url.href, timeout_ms: timeout, wait_ms: wait, js: body.js };
}

export async function withBrowser(callback, timeoutMs = 30_000) {
  const args = ['serve', '--host', '127.0.0.1', '--port', '9222', '--max-connections', '1', '--quiet'];
  if (process.env.PROXY_URL) args.push('--proxy', process.env.PROXY_URL);
  // No API credentials, file access or private-network override in the child.
  const child = spawn(process.env.OBSCURA_BINARY || 'obscura', args, {
    stdio: 'ignore', env: { PATH: process.env.PATH, LANG: 'C.UTF-8', OBSCURA_SCRIPT_DEADLINE_MS: String(timeoutMs) },
  });
  let exited = false;
  let failed = false;
  let stopped = false;
  let cdp;
  let timer;
  const closed = new Promise((resolve) => {
    child.once('error', () => { failed = true; exited = true; resolve(); });
    child.once('exit', () => { exited = true; resolve(); });
  });
  try {
    return await Promise.race([
      (async () => {
        const startupDeadline = Date.now() + 5000;
        while (Date.now() < startupDeadline) {
          if (failed || exited || stopped) throw new Error('Obscura failed to start');
          try {
            const ready = await fetch('http://127.0.0.1:9222/json/version', { signal: AbortSignal.timeout(250) });
            if (ready.ok) break;
          } catch { /* process is still binding */ }
          await sleep(50);
        }
        if (exited || stopped) throw new Error('Obscura exited before connecting');
        cdp = await Cdp.connect('ws://127.0.0.1:9222/devtools/browser');
        if (stopped) { cdp.close(); throw new Error('Browser deadline exceeded'); }
        return await callback(cdp, await cdp.page());
      })(),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Browser operation deadline exceeded; not retried')), timeoutMs); }),
    ]);
  } finally {
    stopped = true;
    clearTimeout(timer);
    cdp?.close();
    if (!exited) child.kill('SIGTERM');
    const force = setTimeout(() => { if (!exited) child.kill('SIGKILL'); }, 1000);
    await closed;
    clearTimeout(force);
  }
}

export async function operate(path, params) {
  return withBrowser(async (cdp, page) => {
    let status = 0;
    cdp.on('Network.responseReceived', (event, sessionId) => {
      if (sessionId === page.sessionId && event.type === 'Document') status = event.response.status;
    });
    const result = await page.send('Page.navigate', { url: params.url });
    if (result.errorText) throw new Error('Navigation failed');
    if (params.wait_ms) await sleep(params.wait_ms);
    const state = await page.evaluate('({url:location.href,html:document.documentElement.outerHTML,title:document.title})');
    if (!state?.html || !state.url?.startsWith('http')) throw new Error('Browser did not produce a document');
    if (path === '/eval') return { engine: 'obscura', status, url: state.url, result: await page.evaluate(params.js) };
    return { engine: 'obscura', status, ...state };
  }, params.timeout_ms);
}

function authorized(header, key) {
  const expected = Buffer.from(`Bearer ${key}`);
  const provided = Buffer.from(typeof header === 'string' ? header : '');
  return provided.length === expected.length && timingSafeEqual(provided, expected);
}

export function createApp({ apiKey, run = operate, ready = () => true }) {
  if (!apiKey) throw new Error('API_KEY is required');
  let busy = false;
  return createServer(async (req, res) => {
    const reply = (status, body) => { res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' }); res.end(JSON.stringify(body)); };
    if (req.url === '/healthz' && req.method === 'GET') {
      reply(ready() ? 200 : 503, { engine: 'obscura', version: VERSION, ready: ready(), busy, form_submission_supported: false });
      return;
    }
    if (!authorized(req.headers.authorization, apiKey)) return reply(401, { error: 'Unauthorized' });
    // Fail BEFORE opening the target. The engine's incorrect GET events must
    // never make a performed POST appear as zero attempts in a caller's ledger.
    if (req.url === '/form-submit') return reply(501, { engine: 'obscura', ok: false, form_submissions: 0, error: FORM_BLOCK_REASON });
    if (!['/render', '/eval'].includes(req.url) || req.method !== 'POST') return reply(404, { error: 'Unknown endpoint' });
    if (!ready()) return reply(503, { error: 'Browser is not ready' });
    if (busy) return reply(429, { error: 'Browser is busy; no work started' });
    busy = true;
    try {
      let size = 0;
      const chunks = [];
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 64 * 1024) return reply(413, { error: 'Request exceeds 64 KiB' });
        chunks.push(chunk);
      }
      let params;
      try { params = validateInput(JSON.parse(Buffer.concat(chunks).toString()), req.url); }
      catch { return reply(400, { error: 'Invalid request parameters' }); }
      const result = await run(req.url, params);
      reply(200, result);
    } catch {
      // No page contents, proxy URLs, code or credentials in service logs.
      reply(502, { error: 'Obscura operation failed or timed out; not retried' });
    } finally { busy = false; }
  });
}

async function main() {
  const port = Number(process.env.PORT);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('PORT is required');
  let ready = false;
  const server = createApp({ apiKey: process.env.API_KEY, ready: () => ready });
  server.requestTimeout = 15_000;
  server.headersTimeout = 10_000;
  server.listen(port, '0.0.0.0');
  await withBrowser(async (_, page) => {
    if (await page.evaluate('1 + 1') !== 2) throw new Error('JavaScript readiness check failed');
  }, 10_000);
  ready = true;
  console.log(`Obscura ${VERSION}: render/eval ready; form submissions disabled by compatibility gate`);
  for (const signal of ['SIGTERM', 'SIGINT']) process.once(signal, () => {
    ready = false;
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 5000).unref();
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(() => { console.error('Obscura startup failed'); process.exit(1); });
}
