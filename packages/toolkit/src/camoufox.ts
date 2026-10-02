// Client for the Camoufox sidecar (services/camoufox): stealth Firefox on an
// ITALIAN residential exit, geoip-coherent (locale and timezone follow the
// exit IP). Stealth-patched Chromium was flagged by Akamai even through an
// Italian residential IP; Camoufox was not — hence Italian sources route here.
//
// It also owns what Scrapling cannot do: a binary fetch through that exit
// (PDFs), a warmed-session in-page fetch for Akamai-gated POSTs, and forms.

import { Config } from './config.js';
import { log } from './log.js';
import { createSidecar, SidecarError } from './sidecar.js';

export class CamoufoxError extends SidecarError {}

export const camoufox = createSidecar({
  name: 'camoufox',
  url: () => Config.camoufox.url,
  error: (m, s, d) => new CamoufoxError(m, s, d),
  onTrip: (reason) =>
    log(`[camoufox] unreachable (${reason}); skipping it for 60s. Fetches fall back to Scrapling.`),
});

const call = <T>(path: string, body: Record<string, unknown>, clientTimeoutMs: number) =>
  camoufox.post<T>(path, body, clientTimeoutMs);

// Every FORM call goes to the dedicated forms service (CAMOUFOX_FORMS_URL;
// falls back to CAMOUFOX_URL). Its own breaker: a forms outage must not
// demote the readers, nor the other way round. Never a fallback to Scrapling.
export const camoufoxForms = createSidecar({
  name: 'camoufox-forms',
  url: () => Config.camoufoxForms.url,
  error: (m, s, d) => new CamoufoxError(m, s, d),
  onTrip: (reason) => log(`[camoufox-forms] unreachable (${reason}); skipping it for 60s.`),
});

const formsCall = <T>(path: string, body: Record<string, unknown>, clientTimeoutMs: number) =>
  camoufoxForms.post<T>(path, body, clientTimeoutMs);

export type CamoufoxRender = { status: number; url: string; html: string };
export type CamoufoxScreenshot = { status: number; url: string; b64: string };
export type CamoufoxEval = { status: number; url: string; result: unknown };
export type CamoufoxBytes = { status: number; b64: string };
export type CamoufoxFormSubmit = {
  contract_version: number; form_submissions: number; error: string | null; status: number; url: string; html: string; ok: boolean;
  exit_session: string; diagnostics: Record<string, unknown>;
  /** With retry_on_captcha_rejection: one entry per attempt; form_submissions is then the total. */
  attempts?: Array<Record<string, unknown>> | null;
};
export type CamoufoxSpaFetch = { status: number; text: string };

export type CamoufoxFormInspect = {
  ok: boolean;
  error: string | null;
  form_submissions: 0;
  status: number;
  url: string;
  forms: Array<Record<string, unknown>>;
  captcha: Array<Record<string, unknown>>;
  cookie_banners: Array<Record<string, unknown>>;
  wizard: Record<string, unknown>;
  diagnostics: Record<string, unknown>;
  exit_session: string;
};

/** Fully-rendered DOM through the Italian residential exit. */
export function camoufoxRender(params: {
  url: string;
  waitUntil?: string;
  waitMs?: number;
  timeoutMs?: number;
  clickAll?: string[];
  settleMs?: number;
  freshIp?: boolean;
}): Promise<CamoufoxRender> {
  const timeoutMs = params.timeoutMs ?? 60_000;
  return call<CamoufoxRender>(
    '/render',
    {
      url: params.url,
      ...(params.waitUntil ? { wait_until: params.waitUntil } : {}),
      ...(params.waitMs !== undefined ? { wait_ms: params.waitMs } : {}),
      timeout_ms: timeoutMs,
      ...(params.clickAll?.length ? { click_all: params.clickAll } : {}),
      ...(params.settleMs !== undefined ? { settle_ms: params.settleMs } : {}),
      ...(params.freshIp ? { fresh_ip: true } : {}),
    },
    timeoutMs + 30_000,
  );
}

export function camoufoxScreenshot(params: {
  url: string;
  waitUntil?: string;
  waitMs?: number;
  timeoutMs?: number;
  fullPage?: boolean;
  width?: number;
  height?: number;
  clickAll?: string[];
  settleMs?: number;
  freshIp?: boolean;
}): Promise<CamoufoxScreenshot> {
  const timeoutMs = params.timeoutMs ?? 90_000;
  return call<CamoufoxScreenshot>(
    '/screenshot',
    {
      url: params.url,
      ...(params.waitUntil ? { wait_until: params.waitUntil } : {}),
      ...(params.waitMs !== undefined ? { wait_ms: params.waitMs } : {}),
      timeout_ms: timeoutMs,
      ...(params.fullPage !== undefined ? { full_page: params.fullPage } : {}),
      ...(params.width ? { width: params.width } : {}),
      ...(params.height ? { height: params.height } : {}),
      ...(params.clickAll?.length ? { click_all: params.clickAll } : {}),
      ...(params.settleMs !== undefined ? { settle_ms: params.settleMs } : {}),
      ...(params.freshIp ? { fresh_ip: true } : {}),
    },
    timeoutMs + 30_000,
  );
}

export function camoufoxEval(params: {
  url: string;
  js: string;
  waitUntil?: string;
  waitMs?: number;
  timeoutMs?: number;
  freshIp?: boolean;
}): Promise<CamoufoxEval> {
  const timeoutMs = params.timeoutMs ?? 90_000;
  return call<CamoufoxEval>(
    '/eval',
    {
      url: params.url,
      js: params.js,
      ...(params.waitUntil ? { wait_until: params.waitUntil } : {}),
      ...(params.waitMs !== undefined ? { wait_ms: params.waitMs } : {}),
      timeout_ms: timeoutMs,
      ...(params.freshIp ? { fresh_ip: true } : {}),
    },
    timeoutMs + 30_000,
  );
}

export type FormFieldSpec = { selector: string; value?: string; action?: 'type' | 'check' | 'select' };

/** Single-attempt form execution; the caller owns durable reservations.
 * No retries: a lost response may conceal a successful submission. */
export function camoufoxFormSubmit(params: {
  url: string;
  fields: FormFieldSpec[];
  submit: string;
  dismiss?: string[];
  successUrl?: string;
  submissionUrls?: string[];
  captchaField?: string;
  requireCaptchaToken?: boolean;
  readyExpression?: string;
  inspectOnly?: boolean;
  waitUntil?: string;
  waitMs?: number;
  settleMs?: number;
  timeoutMs?: number;
  freshIp?: boolean;
  /** Pin the exit: the same token lands on the same IP, so a passing exit is
   *  reused instead of re-searched (the verdict is binary and stable). */
  exitSession?: string;
  /** Headed browser under xvfb for score-gated forms (reCAPTCHA v3 refuses
   *  the headless fingerprint). Default headless; other readers unaffected. */
  headed?: boolean;
  /** Wizard: regex on the business-email gate's button text — clicked ONCE
   *  after step0 when it renders. */
  gateText?: string;
  /** Wizard: the second step's fields, filled+submitted only when it renders. */
  step2?: FormFieldSpec[];
  /** Wizard: step2's submit selector (default 'form button'). */
  step2Submit?: string;
  /** Wizard: body-text regexes; a match is completion even on the same URL. */
  completionMarkers?: string[];
  /** Named persistent profile (warm cookies/fingerprint); omit = isolated. */
  profile?: string;
  /** Wizard warm-up: stop once this many POSTs were answered (1-3). */
  stopAfterPosts?: number;
  /** With a profile: reuse the exit pinned in its fingerprint.json (pin one
   *  on first use). Omit = the sidecar's FORM_PROFILE_STICKY_EXIT. */
  stickyExit?: boolean;
  /** Score-gate the exit on our oracle before the form (the sidecar's default:
   *  on when headed and no exitSession is pinned). false disables it. */
  scoreGate?: boolean;
  scoreThreshold?: number;
  scoreGateTries?: number;
  /** The score oracle the gate probes (Config.oracleUrl). */
  oracleUrl?: string;
  /** Fresh attempts after an explicit step-0 CAPTCHA refusal only (0-4;
   *  the sidecar decides, form_retry.py). */
  retryOnCaptchaRejection?: number;
  /** Regex every error node must match to count as that refusal. */
  captchaRejectionText?: string;
}): Promise<CamoufoxFormSubmit> {
  const timeoutMs = params.timeoutMs ?? 120_000;
  return formsCall<CamoufoxFormSubmit>(
    '/form-submit',
    {
      url: params.url,
      fields: params.fields,
      submit: params.submit,
      ...(params.dismiss ? { dismiss: params.dismiss } : {}),
      ...(params.successUrl ? { success_url: params.successUrl } : {}),
      ...(params.submissionUrls ? { submission_urls: params.submissionUrls } : {}),
      ...(params.captchaField ? { captcha_field: params.captchaField } : {}),
      ...(params.requireCaptchaToken ? { require_captcha_token: true } : {}),
      ...(params.readyExpression ? { ready_expression: params.readyExpression } : {}),
      ...(params.inspectOnly ? { inspect_only: true } : {}),
      ...(params.waitUntil ? { wait_until: params.waitUntil } : {}),
      ...(params.waitMs !== undefined ? { wait_ms: params.waitMs } : {}),
      ...(params.settleMs !== undefined ? { settle_ms: params.settleMs } : {}),
      timeout_ms: timeoutMs,
      fresh_ip: params.freshIp !== false,
      ...(params.exitSession ? { exit_session: params.exitSession } : {}),
      ...(params.headed ? { headed: true } : {}),
      ...(params.gateText ? { gate_text: params.gateText } : {}),
      ...(params.step2 ? { step2: params.step2 } : {}),
      ...(params.step2Submit ? { step2_submit: params.step2Submit } : {}),
      ...(params.completionMarkers ? { completion_markers: params.completionMarkers } : {}),
      ...(params.profile ? { profile: params.profile } : {}),
      ...(params.stopAfterPosts ? { stop_after_posts: params.stopAfterPosts } : {}),
      ...(params.stickyExit !== undefined ? { sticky_exit: params.stickyExit } : {}),
      ...(params.scoreGate !== undefined ? { score_gate: params.scoreGate } : {}),
      ...(params.scoreThreshold !== undefined ? { score_threshold: params.scoreThreshold } : {}),
      ...(params.scoreGateTries !== undefined ? { score_gate_tries: params.scoreGateTries } : {}),
      ...(params.oracleUrl ? { oracle_url: params.oracleUrl } : {}),
      ...(params.retryOnCaptchaRejection ? { retry_on_captcha_rejection: params.retryOnCaptchaRejection } : {}),
      ...(params.captchaRejectionText ? { captcha_rejection_text: params.captchaRejectionText } : {}),
    },
    timeoutMs + 60_000,
  );
}

/** Stealth-score diagnostics (services/camoufox/score_probe.py): the body is
 *  passed through in snake_case; the client waits a minute past timeout_ms. */
export function camoufoxStealth(path: string, body: Record<string, unknown>): Promise<Record<string, unknown>> {
  const timeoutMs = typeof body.timeout_ms === 'number' ? body.timeout_ms : 180_000;
  return formsCall<Record<string, unknown>>(path, body, timeoutMs + 60_000);
}

/** Read-only: the page's forms, fields, submit, CAPTCHA, honeypots, banners.
 * Never fills or clicks; every mutating request is aborted. */
export function camoufoxFormInspect(params: {
  url: string;
  waitUntil?: string;
  waitMs?: number;
  timeoutMs?: number;
  freshIp?: boolean;
  exitSession?: string;
  headed?: boolean;
  profile?: string;
}): Promise<CamoufoxFormInspect> {
  const timeoutMs = params.timeoutMs ?? 60_000;
  return formsCall<CamoufoxFormInspect>(
    '/form-inspect',
    {
      url: params.url,
      ...(params.waitUntil ? { wait_until: params.waitUntil } : {}),
      ...(params.waitMs !== undefined ? { wait_ms: params.waitMs } : {}),
      timeout_ms: timeoutMs,
      fresh_ip: params.freshIp !== false,
      ...(params.exitSession ? { exit_session: params.exitSession } : {}),
      ...(params.headed ? { headed: true } : {}),
      ...(params.profile ? { profile: params.profile } : {}),
    },
    timeoutMs + 60_000,
  );
}

/** Binary fetch (PDFs) through the residential exit. Returns base64. */
export function camoufoxBytes(params: { url: string; timeoutMs?: number }): Promise<CamoufoxBytes> {
  const timeoutMs = params.timeoutMs ?? 60_000;
  return call<CamoufoxBytes>('/bytes', { url: params.url, timeout_ms: timeoutMs }, timeoutMs + 30_000);
}

/**
 * Same-origin in-page fetch on a warmed page, for origins whose POSTs are gated
 * on an Akamai sensor cookie.
 *
 * This one is stateful in a way nothing else here is: the sidecar keeps ONE
 * warmed page per (base_url, warm_path), pins it to a sticky proxy exit, and
 * feeds the sensor on a keepalive so `_abck` stays validated. Callers driving a
 * long crawl should treat the warmed session as a shared resource — a
 * `camoufoxRecycle()` or a render that tears the browser down will cost them
 * their maturation.
 */
export function camoufoxSpaFetch(params: {
  baseUrl: string;
  warmPath?: string;
  method?: string;
  path: string;
  body?: Record<string, unknown> | null;
  accept?: string;
  sensorWaitMs?: number;
  maturProbe?: Record<string, unknown> | null;
  maturMaxTries?: number;
  timeoutMs?: number;
}): Promise<CamoufoxSpaFetch> {
  // The sidecar's own in-page deadline is ~45s and maturation can loop several
  // times on top of that, so this needs a much longer client budget than a
  // plain render.
  const timeoutMs = params.timeoutMs ?? 180_000;
  return call<CamoufoxSpaFetch>(
    '/spa-fetch',
    {
      base_url: params.baseUrl,
      ...(params.warmPath ? { warm_path: params.warmPath } : {}),
      ...(params.method ? { method: params.method } : {}),
      path: params.path,
      ...(params.body !== undefined ? { body: params.body } : {}),
      ...(params.accept ? { accept: params.accept } : {}),
      ...(params.sensorWaitMs !== undefined ? { sensor_wait_ms: params.sensorWaitMs } : {}),
      ...(params.maturProbe !== undefined ? { mature_probe: params.maturProbe } : {}),
      ...(params.maturMaxTries !== undefined ? { mature_max_tries: params.maturMaxTries } : {}),
    },
    timeoutMs,
  );
}

/** Drop the warmed Akamai session and the render browser (full relaunch). */
export function camoufoxRecycle(): Promise<{ ok: boolean }> {
  return call<{ ok: boolean }>('/recycle', {}, 120_000);
}
