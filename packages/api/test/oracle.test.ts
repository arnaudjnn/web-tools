// Oracle verify parsing with a mocked siteverify (no network, no Google).
import assert from 'node:assert/strict';
import { test } from 'vitest';
import {
  allowVerify,
  oracleConfig,
  parseSiteverify,
  renderOraclePage,
  renderVerdict,
  verifyToken,
} from '../src/oracle.js';

const NOW = Date.parse('2026-10-02T10:00:30Z');

test('a passing siteverify body is normalized', () => {
  const v = parseSiteverify(
    { success: true, score: 0.9, action: 'user_registration_production', hostname: 'tools.example', challenge_ts: '2026-10-02T10:00:00Z', 'error-codes': [] },
    'user_registration_production',
    NOW,
  );
  assert.equal(v.success, true);
  assert.equal(v.score, 0.9);
  assert.equal(v.action_ok, true);
  assert.equal(v.hostname, 'tools.example');
  assert.equal(v.token_age_s, 30);
  assert.deepEqual(v['error-codes'], []);
});

test('an action mismatch is flagged, not hidden', () => {
  const v = parseSiteverify({ success: true, score: 0.7, action: 'login' }, 'user_registration_production', NOW);
  assert.equal(v.action_ok, false);
});

test('odd shapes never throw and never invent a score', () => {
  for (const raw of [null, 'x', 42, [], { success: 'true', score: '0.9' }, { score: Number.NaN }]) {
    const v = parseSiteverify(raw, null, NOW);
    assert.equal(v.success, false);
    assert.equal(v.score, null);
    assert.equal(v.action_ok, null);
  }
});

test('error codes keep only strings', () => {
  const v = parseSiteverify({ success: false, 'error-codes': ['timeout-or-duplicate', 3, null] }, null, NOW);
  assert.deepEqual(v['error-codes'], ['timeout-or-duplicate']);
});

test('verifyToken posts secret+response+remoteip as a form and parses the answer', async () => {
  let seen: { url: string; body: string; ct: string } | null = null;
  const fake = (async (url: string, init: RequestInit) => {
    seen = { url, body: String(init.body), ct: (init.headers as Record<string, string>)['Content-Type'] ?? '' };
    return new Response(JSON.stringify({ success: true, score: 0.3, action: 'a' }), { status: 200 });
  }) as unknown as typeof fetch;
  const v = await verifyToken({ token: 'TOK', secret: 'SEC', expectedAction: 'a', remoteip: '1.2.3.4', fetchImpl: fake });
  assert.equal(v.score, 0.3);
  assert.ok(seen);
  const s = seen as { url: string; body: string; ct: string };
  assert.equal(s.url, 'https://www.google.com/recaptcha/api/siteverify');
  assert.equal(s.ct, 'application/x-www-form-urlencoded');
  const form = new URLSearchParams(s.body);
  assert.equal(form.get('secret'), 'SEC');
  assert.equal(form.get('response'), 'TOK');
  assert.equal(form.get('remoteip'), '1.2.3.4');
});

test('an empty token never calls Google', async () => {
  const fake = (async () => {
    throw new Error('must not be called');
  }) as unknown as typeof fetch;
  const v = await verifyToken({ token: '', secret: 'SEC', expectedAction: null, fetchImpl: fake });
  assert.deepEqual(v['error-codes'], ['missing-input-response']);
});

test('siteverify HTTP errors and network failures are verdicts, not throws', async () => {
  const http500 = (async () => new Response('', { status: 500 })) as unknown as typeof fetch;
  assert.deepEqual((await verifyToken({ token: 't', secret: 's', expectedAction: null, fetchImpl: http500 }))['error-codes'], ['siteverify-http-500']);
  const down = (async () => {
    throw new TypeError('fetch failed');
  }) as unknown as typeof fetch;
  assert.deepEqual((await verifyToken({ token: 't', secret: 's', expectedAction: null, fetchImpl: down }))['error-codes'], ['siteverify-unreachable']);
});

test('disabled unless both sitekey and secret are set; bad action falls back', () => {
  assert.equal(oracleConfig({}), null);
  assert.equal(oracleConfig({ RECAPTCHA_ORACLE_SITEKEY: 'k' }), null);
  assert.equal(oracleConfig({ RECAPTCHA_ORACLE_SECRET: 's' }), null);
  assert.deepEqual(oracleConfig({ RECAPTCHA_ORACLE_SITEKEY: 'k', RECAPTCHA_ORACLE_SECRET: 's', RECAPTCHA_ORACLE_ACTION: 'bad action!' }), {
    sitekey: 'k', secret: 's', action: 'user_registration_production',
  });
});

test('the page mints inside the submit handler (django-recaptcha v3 shape)', () => {
  const html = renderOraclePage('SITEKEY123', 'user_registration_production');
  assert.match(html, /api\.js\?render=SITEKEY123/);
  assert.match(html, /addEventListener\('submit'/);
  assert.match(html, /grecaptcha\.execute\("SITEKEY123", \{action: "user_registration_production"\}\)/);
  assert.match(html, /name="g-recaptcha-response"/);
});

test('the handler submits through the prototype: id="submit" shadows form.submit', () => {
  const html = renderOraclePage('K', 'a');
  // The probe clicks #submit, so the control keeps that id...
  assert.match(html, /<button id="submit" type="submit">/);
  // ...which makes form.submit the BUTTON; a bare form.submit() throws.
  assert.doesNotMatch(html, /\.form\.submit\(\)/);
  assert.match(html, /HTMLFormElement\.prototype\.submit/);
  assert.match(html, /nativeSubmit\.call\(element\.form\)/);
});

test('the verdict node is parseable and cannot break out of its script tag', () => {
  const html = renderVerdict({ success: true, score: 0.9, hostname: '</script><b>' });
  const m = html.match(/<script type="application\/json" id="oracle-verdict">([\s\S]*?)<\/script>/);
  assert.ok(m);
  assert.equal(JSON.parse(m![1]!).hostname, '</script><b>');
});

test('the verify limiter caps a minute window', () => {
  const t = 10_000_000;
  let allowed = 0;
  for (let i = 0; i < 40; i++) if (allowVerify(t, 30)) allowed++;
  assert.equal(allowed, 30);
  assert.equal(allowVerify(t + 60_000, 30), true);
});

test('a rejected or empty execute() is reported, not silent', () => {
  const html = renderOraclePage('K', 'a');
  assert.match(html, /name="mint_error"/);
  assert.match(html, /'rejected:' \+ String\(err/);
  assert.match(html, /'resolved-empty'/);
});
