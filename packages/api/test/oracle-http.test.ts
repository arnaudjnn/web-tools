// The oracle's HTTP routes (mountOracle) on a loopback Express app: the page,
// the verify with siteverify mocked at global fetch, the rate limit, the
// disabled-when-unset 404, and that neither the secret nor the token leaks.
import type { Server } from 'node:http';
import express from 'express';
import { afterEach, describe, expect, it, vi } from 'vitest';

const realFetch = globalThis.fetch;
const SITEKEY = 'SITEKEY-public-123';
const SECRET = 'SECRET-never-shown-456';
const TOKEN = 'TOKEN-single-use-789';
const SITEVERIFY = 'https://www.google.com/recaptcha/api/siteverify';

type Google = (body: URLSearchParams) => Response | Promise<Response>;

let server: Server | undefined;

/** A fresh module (fresh limiter, config read at mount) on its own port. */
async function boot(env: Record<string, string | undefined>, google: Google = () => Response.json({ success: true })) {
  vi.resetModules();
  for (const [k, v] of Object.entries(env)) vi.stubEnv(k, v as string);
  const calls: URLSearchParams[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: URL | string, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith('http://127.0.0.1')) return realFetch(input, init);
      expect(url).toBe(SITEVERIFY);
      const body = new URLSearchParams(String(init?.body));
      calls.push(body);
      return google(body);
    }),
  );
  const { mountOracle } = await import('../src/oracle.js');
  const logs: unknown[][] = [];
  const app = express();
  mountOracle(app, (...a) => void logs.push(a));
  server = await new Promise<Server>((r) => {
    const s = app.listen(0, '127.0.0.1', () => r(s));
  });
  const base = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  const verify = (fields: Record<string, string>, headers: Record<string, string> = {}) =>
    realFetch(`${base}/oracle/recaptcha/verify`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', ...headers },
      body: new URLSearchParams(fields).toString(),
    });
  return { base, calls, logs, verify, get: (q = '') => realFetch(`${base}/oracle/recaptcha${q}`) };
}

const verdictOf = (html: string) => {
  const m = html.match(/<script type="application\/json" id="oracle-verdict">([\s\S]*?)<\/script>/);
  expect(m).not.toBeNull();
  return JSON.parse(m![1]!);
};

const ON = { RECAPTCHA_ORACLE_SITEKEY: SITEKEY, RECAPTCHA_ORACLE_SECRET: SECRET, RECAPTCHA_ORACLE_ACTION: undefined };

afterEach(async () => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.useRealTimers();
  await new Promise((r) => (server ? server.close(r) : r(undefined)));
  server = undefined;
});

describe('disabled unless both sitekey and secret are set', () => {
  for (const env of [
    { RECAPTCHA_ORACLE_SITEKEY: undefined, RECAPTCHA_ORACLE_SECRET: undefined },
    { RECAPTCHA_ORACLE_SITEKEY: SITEKEY, RECAPTCHA_ORACLE_SECRET: undefined },
    { RECAPTCHA_ORACLE_SITEKEY: undefined, RECAPTCHA_ORACLE_SECRET: SECRET },
  ]) {
    it(`404s the page and the verify (${Object.entries(env).filter(([, v]) => v).map(([k]) => k).join('+') || 'none'} set)`, async () => {
      const o = await boot(env);
      const page = await o.get();
      expect(page.status).toBe(404);
      expect(await page.json()).toEqual({ error: 'oracle disabled (RECAPTCHA_ORACLE_SITEKEY/SECRET unset)' });
      const v = await o.verify({ 'g-recaptcha-response': TOKEN });
      expect(v.status).toBe(404);
      expect(o.calls).toHaveLength(0);
    });
  }
});

describe('GET the page', () => {
  it('serves the page with the sitekey and the default action, uncached, never the secret', async () => {
    const o = await boot(ON);
    const r = await o.get();
    expect(r.status).toBe(200);
    expect(r.headers.get('content-type')).toMatch(/^text\/html/);
    expect(r.headers.get('cache-control')).toBe('no-store');
    const html = await r.text();
    expect(html).toContain(`api.js?render=${SITEKEY}`);
    expect(html).toContain(`grecaptcha.execute("${SITEKEY}", {action: "user_registration_production"})`);
    expect(html).toContain('action="/oracle/recaptcha/verify"');
    expect(html).not.toContain(SECRET);
  });

  it('?action= picks the action when valid; an invalid one falls back to the configured action', async () => {
    const o = await boot({ ...ON, RECAPTCHA_ORACLE_ACTION: 'signup_v2' });
    expect(await (await o.get('?action=login')).text()).toContain('{action: "login"}');
    const bad = await (await o.get('?action=%3Cscript%3E')).text();
    expect(bad).toContain('{action: "signup_v2"}');
    expect(bad).not.toContain('<script>"');
  });
});

describe('POST verify', () => {
  it('a pass: score, action, hostname and timings, with the secret and token sent only to Google', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-10-02T10:00:12Z'));
    const o = await boot(ON, () =>
      Response.json({ success: true, score: 0.9, action: 'user_registration_production', hostname: 'tools.example', challenge_ts: '2026-10-02T10:00:00Z' }),
    );
    const r = await o.verify(
      { 'g-recaptcha-response': TOKEN, expected_action: 'user_registration_production', t_load: '1000', t_submit: '9000', t_mint: '450', company: 'x' },
      { 'X-Forwarded-For': '93.40.1.2, 10.0.0.1' },
    );
    expect(r.status).toBe(200);
    expect(r.headers.get('cache-control')).toBe('no-store');
    const html = await r.text();
    expect(verdictOf(html)).toEqual({
      success: true,
      score: 0.9,
      action: 'user_registration_production',
      hostname: 'tools.example',
      challenge_ts: '2026-10-02T10:00:00Z',
      'error-codes': [],
      expected_action: 'user_registration_production',
      action_ok: true,
      token_age_s: 12,
      client_ip: '93.40.1.2',
      token_length: TOKEN.length,
      page_dwell_s: 8,
      mint_s: 0.5,
      mint_error: null,
    });
    // Google got the secret, the token and the exit IP ...
    expect(Object.fromEntries(o.calls[0]!)).toEqual({ secret: SECRET, response: TOKEN, remoteip: '93.40.1.2' });
    // ... and nothing that leaves Tools carries the secret or the token.
    expect(html).not.toContain(SECRET);
    expect(html).not.toContain(TOKEN);
    expect(JSON.stringify(o.logs)).not.toContain(SECRET);
    expect(JSON.stringify(o.logs)).not.toContain(TOKEN);
    expect(o.logs[0]![0]).toBe('oracle verdict');
    expect(o.logs[0]![1]).toMatchObject({ success: true, score: 0.9, action_ok: true, ip: '93.40.1.2', dwell: 8 });
  });

  it('a low score on the wrong action is reported as such', async () => {
    const o = await boot(ON, () => Response.json({ success: true, score: 0.1, action: 'login' }));
    const v = verdictOf(await (await o.verify({ 'g-recaptcha-response': TOKEN })).text());
    expect(v).toMatchObject({ success: true, score: 0.1, action: 'login', expected_action: 'user_registration_production', action_ok: false });
    expect(v).toMatchObject({ page_dwell_s: null, mint_s: null, token_age_s: null });
  });

  it("Google's error-codes come through", async () => {
    const o = await boot(ON, () => Response.json({ success: false, 'error-codes': ['timeout-or-duplicate'] }));
    const v = verdictOf(await (await o.verify({ 'g-recaptcha-response': TOKEN }, { 'X-Real-IP': '5.6.7.8' })).text());
    expect(v).toMatchObject({ success: false, score: null, 'error-codes': ['timeout-or-duplicate'], client_ip: '5.6.7.8' });
  });

  it('Google unreachable or answering 5xx is a verdict, not a crash', async () => {
    const down = await boot(ON, () => {
      throw new TypeError('fetch failed');
    });
    const v = verdictOf(await (await down.verify({ 'g-recaptcha-response': TOKEN })).text());
    expect(v).toMatchObject({ success: false, 'error-codes': ['siteverify-unreachable'] });
    // No proxy headers: the socket address is the client IP.
    expect(v.client_ip).toMatch(/127\.0\.0\.1/);
    await new Promise((r) => server!.close(r));

    const http = await boot(ON, () => new Response('', { status: 503 }));
    const r = await http.verify({ 'g-recaptcha-response': TOKEN });
    expect(r.status).toBe(200);
    expect(verdictOf(await r.text())['error-codes']).toEqual(['siteverify-http-503']);
  });

  it('a missing token never calls Google; a bad expected_action falls back to the configured one', async () => {
    const o = await boot(ON);
    const v = verdictOf(await (await o.verify({ expected_action: 'not valid!', t_mint: '-1' })).text());
    expect(v).toMatchObject({ success: false, 'error-codes': ['missing-input-response'], token_length: 0, expected_action: 'user_registration_production', mint_s: 0 });
    expect(o.calls).toHaveLength(0);
  });

  it('rate-limits at 30 verifies a minute, without calling Google past the cap, then resets', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-10-02T10:00:00Z'));
    const o = await boot(ON, () => Response.json({ success: true, score: 0.9 }));
    const statuses: number[] = [];
    for (let i = 0; i < 32; i++) statuses.push((await o.verify({ 'g-recaptcha-response': TOKEN })).status);
    expect(statuses.filter((s) => s === 200)).toHaveLength(30);
    expect(statuses.slice(30)).toEqual([429, 429]);
    expect(o.calls).toHaveLength(30);
    const limited = await o.verify({ 'g-recaptcha-response': TOKEN });
    expect(limited.status).toBe(429);
    expect(verdictOf(await limited.text())).toEqual({ success: false, 'error-codes': ['oracle-rate-limited'] });

    vi.setSystemTime(new Date('2026-10-02T10:01:00Z'));
    expect((await o.verify({ 'g-recaptcha-response': TOKEN })).status).toBe(200);
  });
});
