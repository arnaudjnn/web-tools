// Client for the Camoufox sidecar (services/camoufox).
//
// Camoufox is a stealth *Firefox* on an Italian residential exit, and it is not
// interchangeable with the other two backends:
//
//   Crawl4AI   headless Chromium, this host's datacenter IP, no proxy possible
//   Scrapling  Patchright Chromium, US residential exit or challenge-solving
//   Camoufox   Firefox, IT residential exit, geoip-coherent, sticky sessions
//
// The distinction is load-bearing rather than cosmetic: stealth-patched headless
// Chrome was flagged by Akamai *even through an Italian residential IP*, while
// Camoufox's fingerprint is internally coherent (its locale and timezone are
// derived from the exit IP via geoip). That is why Italian authority sources go
// here and not through Scrapling.
//
// It also owns two capabilities nothing else here has: a binary fetch through
// the residential exit (PDFs), and a warmed-session in-page fetch for
// Akamai-sensor-gated POSTs.

import { Config } from './config.js';

export class CamoufoxError extends Error {}

async function call<T>(path: string, body: Record<string, unknown>, timeoutMs: number): Promise<T> {
  if (!Config.camoufox.url) {
    throw new CamoufoxError('CAMOUFOX_URL is not configured');
  }

  let response: Response;
  try {
    response = await fetch(new URL(path, Config.camoufox.url), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (err) {
    throw new CamoufoxError(
      `camoufox ${path} unreachable: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  if (!response.ok) {
    const text = await response.text().catch(() => '');
    throw new CamoufoxError(`camoufox ${path} HTTP ${response.status}: ${text.slice(0, 300)}`);
  }

  return (await response.json()) as T;
}

export type CamoufoxRender = { status: number; url: string; html: string };
export type CamoufoxScreenshot = { status: number; url: string; b64: string };
export type CamoufoxEval = { status: number; url: string; result: unknown };
export type CamoufoxBytes = { status: number; b64: string };
export type CamoufoxFormSubmit = { contract_version: number; form_submissions: number; error: string | null; status: number; url: string; html: string; ok: boolean; exit_session: string; diagnostics: Record<string, unknown> };
export type CamoufoxSpaFetch = { status: number; text: string };

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
  inspectOnly?: boolean;
  waitUntil?: string;
  waitMs?: number;
  settleMs?: number;
  timeoutMs?: number;
  freshIp?: boolean;
  /** Pin the exit: the same token lands on the same IP, so a passing exit is
   *  reused instead of re-searched (the verdict is binary and stable). */
  exitSession?: string;
}): Promise<CamoufoxFormSubmit> {
  const timeoutMs = params.timeoutMs ?? 120_000;
  return call<CamoufoxFormSubmit>(
    '/form-submit',
    {
      url: params.url,
      fields: params.fields,
      submit: params.submit,
      ...(params.dismiss ? { dismiss: params.dismiss } : {}),
      ...(params.successUrl ? { success_url: params.successUrl } : {}),
      ...(params.submissionUrls ? { submission_urls: params.submissionUrls } : {}),
      ...(params.captchaField ? { captcha_field: params.captchaField } : {}),
      ...(params.inspectOnly ? { inspect_only: true } : {}),
      ...(params.waitUntil ? { wait_until: params.waitUntil } : {}),
      ...(params.waitMs !== undefined ? { wait_ms: params.waitMs } : {}),
      ...(params.settleMs !== undefined ? { settle_ms: params.settleMs } : {}),
      timeout_ms: timeoutMs,
      fresh_ip: params.freshIp !== false,
      ...(params.exitSession ? { exit_session: params.exitSession } : {}),
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
