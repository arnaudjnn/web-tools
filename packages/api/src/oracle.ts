// reCAPTCHA v3 score oracle — OUR key, OUR page, so the form browser's score
// can be measured without ever touching a third-party form.
//
// WHY TOOLS SERVES IT (not the Camoufox sidecar). A reCAPTCHA key only mints
// tokens on hostnames registered on it, and the sidecar is private
// (camoufox.railway.internal has no public name to register). Tools is the one
// public hostname this project owns, so the page lives here and Camoufox's form
// browser navigates to it THROUGH ITS RESIDENTIAL EXIT exactly as it navigates
// to a real form. Tools also holds the secret and does the siteverify call, so
// the secret never enters the browser host. The probe on the sidecar reads the
// verdict out of the POST's own response page — no API key ever enters the
// browser (the page and its verify are deliberately outside the Bearer auth:
// the sitekey is public by design, and the verify only ever scores OUR key).
//
// The page mirrors the shape that matters (Atoka: django-recaptcha's V3
// widget): api.js?render=<key>, a submit LISTENER that calls
// grecaptcha.execute(key, {action}) inside the handler, writes the token into a
// hidden field and calls form.submit(). Single-use token, ~2 min life.
//
// Disabled cleanly (404, nothing mounted beyond that) unless BOTH
// RECAPTCHA_ORACLE_SITEKEY and RECAPTCHA_ORACLE_SECRET are set.
// Never logs the token or the secret.

import express, { type Express, type Request, type Response } from 'express';

export const ORACLE_PATH = '/oracle/recaptcha';
export const VERIFY_PATH = `${ORACLE_PATH}/verify`;
export const DEFAULT_ACTION = 'user_registration_production';
const ACTION_RE = /^[A-Za-z0-9_/]{1,64}$/;
const SITEVERIFY_URL = 'https://www.google.com/recaptcha/api/siteverify';

export type OracleConfig = { sitekey: string; secret: string; action: string };

export function oracleConfig(env: NodeJS.ProcessEnv = process.env): OracleConfig | null {
  const sitekey = env.RECAPTCHA_ORACLE_SITEKEY?.trim();
  const secret = env.RECAPTCHA_ORACLE_SECRET?.trim();
  if (!sitekey || !secret) return null;
  const action = env.RECAPTCHA_ORACLE_ACTION?.trim();
  return { sitekey, secret, action: action && ACTION_RE.test(action) ? action : DEFAULT_ACTION };
}

export type Verdict = {
  success: boolean;
  score: number | null;
  action: string | null;
  hostname: string | null;
  challenge_ts: string | null;
  'error-codes': string[];
  /** The action the page asked for; `action_ok` is false on a mismatch. */
  expected_action: string | null;
  action_ok: boolean | null;
  /** Token age at verify time, from Google's challenge_ts (seconds). */
  token_age_s: number | null;
  /** The exit IP as Tools saw it (Railway edge X-Forwarded-For). */
  client_ip: string | null;
  token_length: number;
  /** Page-side timings the page reports (ms → s): load → submit, execute(). */
  page_dwell_s: number | null;
  mint_s: number | null;
};

/** Normalize a siteverify JSON body. Pure: never throws on odd shapes. */
export function parseSiteverify(
  raw: unknown,
  expectedAction: string | null,
  nowMs: number = Date.now(),
): Pick<Verdict, 'success' | 'score' | 'action' | 'hostname' | 'challenge_ts' | 'error-codes' | 'expected_action' | 'action_ok' | 'token_age_s'> {
  const body = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  const score = typeof body.score === 'number' && Number.isFinite(body.score) ? body.score : null;
  const action = typeof body.action === 'string' ? body.action : null;
  const challengeTs = typeof body.challenge_ts === 'string' ? body.challenge_ts : null;
  const codes = Array.isArray(body['error-codes'])
    ? (body['error-codes'] as unknown[]).filter((c): c is string => typeof c === 'string')
    : [];
  const parsedTs = challengeTs ? Date.parse(challengeTs) : NaN;
  return {
    success: body.success === true,
    score,
    action,
    hostname: typeof body.hostname === 'string' ? body.hostname : null,
    challenge_ts: challengeTs,
    'error-codes': codes,
    expected_action: expectedAction,
    action_ok: expectedAction && action ? action === expectedAction : null,
    token_age_s: Number.isFinite(parsedTs) ? Math.round((nowMs - parsedTs) / 100) / 10 : null,
  };
}

/** Verify one token with Google. `fetchImpl` is injectable for tests. */
export async function verifyToken(params: {
  token: string;
  secret: string;
  expectedAction: string | null;
  remoteip?: string | null;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}): Promise<ReturnType<typeof parseSiteverify>> {
  if (!params.token) return parseSiteverify({ success: false, 'error-codes': ['missing-input-response'] }, params.expectedAction);
  const form = new URLSearchParams({ secret: params.secret, response: params.token });
  if (params.remoteip) form.set('remoteip', params.remoteip);
  const doFetch = params.fetchImpl ?? fetch;
  try {
    const r = await doFetch(SITEVERIFY_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: form.toString(),
      signal: AbortSignal.timeout(params.timeoutMs ?? 10_000),
    });
    if (!r.ok) return parseSiteverify({ success: false, 'error-codes': [`siteverify-http-${r.status}`] }, params.expectedAction);
    return parseSiteverify(await r.json(), params.expectedAction);
  } catch {
    return parseSiteverify({ success: false, 'error-codes': ['siteverify-unreachable'] }, params.expectedAction);
  }
}

function clientIp(req: Request): string | null {
  const xff = req.headers['x-forwarded-for'];
  const first = (Array.isArray(xff) ? xff[0] : xff)?.split(',')[0]?.trim();
  const real = req.headers['x-real-ip'];
  return first || (Array.isArray(real) ? real[0] : real) || req.socket.remoteAddress || null;
}

const esc = (s: string) =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

export function renderOraclePage(sitekey: string, action: string): string {
  const key = JSON.stringify(sitekey);
  const act = JSON.stringify(action);
  // Fields are dummies with realistic autocompletion hints; the probe types
  // into them with the same humanized keystrokes forms use. The listener is
  // django-recaptcha's widget_v3 shape: mint INSIDE the submit handler.
  return `<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Registrazione</title>
<style>
body{font-family:system-ui,sans-serif;max-width:560px;margin:48px auto;padding:0 16px;color:#1d2433;background:#fff}
label{display:block;margin:14px 0 4px;font-size:14px}input{width:100%;padding:9px;font-size:15px;box-sizing:border-box}
button{margin-top:20px;padding:10px 18px;font-size:15px}p{line-height:1.5;color:#4a5262}
</style></head><body>
<h1>Crea il tuo account</h1>
<p>Compila il modulo per iniziare la prova gratuita. I campi contrassegnati sono obbligatori.</p>
<form id="signup" method="post" action="${VERIFY_PATH}">
<input type="hidden" name="expected_action" value="${esc(action)}">
<input type="hidden" name="t_load" id="t_load" value="">
<input type="hidden" name="t_submit" id="t_submit" value="">
<input type="hidden" name="t_mint" id="t_mint" value="">
<label for="company">Ragione sociale *</label><input id="company" name="company" type="text" autocomplete="organization" required>
<label for="email">Email aziendale</label><input id="email" name="email" type="text" autocomplete="email">
<label for="phone">Telefono</label><input id="phone" name="phone" type="text" autocomplete="tel">
<input type="hidden" name="g-recaptcha-response" id="oracle-token" data-widget-uuid="oracle" value="">
<button id="submit" type="submit">Registrati</button>
</form>
<p style="font-size:12px">Questo sito è protetto da reCAPTCHA.</p>
<script>document.getElementById('t_load').value = String(Date.now());</script>
<script src="https://www.google.com/recaptcha/api.js?render=${encodeURIComponent(sitekey)}"></script>
<script>
// The submit control keeps id="submit" (the probe's selector), and a form
// control with id/name "submit" SHADOWS HTMLFormElement.submit: form.submit
// is then the button, and form.submit() throws inside the mint promise — no
// POST ever leaves (measured live 2026-10-02: submit_clicked, scripts 4/4,
// posts=[]). So call the prototype's submit, which nothing can shadow.
var nativeSubmit = HTMLFormElement.prototype.submit;
grecaptcha.ready(function () {
  var element = document.getElementById('oracle-token');
  element.form.addEventListener('submit', function (event) {
    event.preventDefault();
    var t0 = Date.now();
    document.getElementById('t_submit').value = String(t0);
    grecaptcha.execute(${key}, {action: ${act}}).then(function (token) {
      document.getElementById('t_mint').value = String(Date.now() - t0);
      element.value = token;
      nativeSubmit.call(element.form);
    }, function () {
      document.getElementById('t_mint').value = '-1';  // execute() rejected
      nativeSubmit.call(element.form);  // let the verify report the missing token
    });
  });
});
</script>
</body></html>`;
}

/** The verify answer is a PAGE (the form POST navigates to it) carrying the
 *  verdict as an inert JSON script node the probe parses out of page.content(). */
export function renderVerdict(v: Verdict | Record<string, unknown>): string {
  const json = JSON.stringify(v).replace(/</g, '\\u003c');
  return `<!doctype html><html><head><meta charset="utf-8"><title>Esito</title></head><body>
<h1>Esito verifica</h1>
<script type="application/json" id="oracle-verdict">${json}</script>
<pre>${esc(JSON.stringify(v, null, 2))}</pre></body></html>`;
}

const num = (v: unknown): number | null => {
  const n = typeof v === 'string' && v ? Number(v) : NaN;
  return Number.isFinite(n) ? n : null;
};

// Fixed-window limiter: the verify is public, so a stranger could burn the
// key's quota. 30/min per process is far above any bench (one probe ~30-60s).
let windowStart = 0;
let windowCount = 0;
export function allowVerify(nowMs: number = Date.now(), limit = 30): boolean {
  if (nowMs - windowStart >= 60_000) {
    windowStart = nowMs;
    windowCount = 0;
  }
  windowCount++;
  return windowCount <= limit;
}

/** Mount BEFORE the Bearer middleware: the browser carries no API key. */
export function mountOracle(app: Express, log: (...a: unknown[]) => void): void {
  const cfg = oracleConfig();
  app.get(ORACLE_PATH, (req: Request, res: Response) => {
    if (!cfg) {
      res.status(404).json({ error: 'oracle disabled (RECAPTCHA_ORACLE_SITEKEY/SECRET unset)' });
      return;
    }
    const q = typeof req.query.action === 'string' ? req.query.action : '';
    const action = ACTION_RE.test(q) ? q : cfg.action;
    res.set('Cache-Control', 'no-store').type('html').send(renderOraclePage(cfg.sitekey, action));
  });
  app.post(VERIFY_PATH, express.urlencoded({ extended: false, limit: '64kb' }), async (req: Request, res: Response) => {
    if (!cfg) {
      res.status(404).json({ error: 'oracle disabled (RECAPTCHA_ORACLE_SITEKEY/SECRET unset)' });
      return;
    }
    const body = (req.body ?? {}) as Record<string, unknown>;
    const ip = clientIp(req);
    const token = typeof body['g-recaptcha-response'] === 'string' ? body['g-recaptcha-response'] : '';
    const expected = typeof body.expected_action === 'string' && ACTION_RE.test(body.expected_action)
      ? body.expected_action
      : cfg.action;
    const tLoad = num(body.t_load);
    const tSubmit = num(body.t_submit);
    const tMint = num(body.t_mint);
    const timings = {
      page_dwell_s: tLoad !== null && tSubmit !== null ? Math.round((tSubmit - tLoad) / 100) / 10 : null,
      mint_s: tMint !== null ? Math.round(tMint / 100) / 10 : null,
    };
    if (!allowVerify()) {
      res.status(429).type('html').send(renderVerdict({ success: false, 'error-codes': ['oracle-rate-limited'] }));
      return;
    }
    const parsed = await verifyToken({ token, secret: cfg.secret, expectedAction: expected, remoteip: ip });
    const verdict: Verdict = { ...parsed, client_ip: ip, token_length: token.length, ...timings };
    // Score + shape only; never the token.
    log('oracle verdict', {
      success: verdict.success, score: verdict.score, action_ok: verdict.action_ok,
      codes: verdict['error-codes'], ip: verdict.client_ip, dwell: verdict.page_dwell_s,
    });
    res.set('Cache-Control', 'no-store').type('html').send(renderVerdict(verdict));
  });
}
