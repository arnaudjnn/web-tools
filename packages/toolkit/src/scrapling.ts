// Client for the Scrapling sidecar (services/scrapling).
//
// After the Crawl4AI removal (benchmarked 2026-09-27, see AGENTS.md) the
// sidecar is the whole fetch/render/capture pipeline except the Italian
// residential path:
//
//   /fetch       pages: residential egress, JS-challenge solving
//   /markdown    HTML→markdown, pure CPU (no browser)
//   /raw         plain HTTP GET (CDX JSON, archived pages) — no browser
//   /screenshot  /pdf /eval through one shared browser session per mode
//
// Camoufox keeps the Italian residential exit and is the fallback for fetches.

import { Config } from './config.js';

export type ScraplingMode = 'fast' | 'stealth' | 'solve';

export type ScraplingResult = {
  status: number;
  url: string;
  html: string;
  size: number;
  /** The mode that actually served the response, which may not be the one asked for. */
  mode: ScraplingMode;
  /** True when the sidecar auto-retried in `solve` after detecting a challenge. */
  escalated: boolean;
};

export type ScraplingCapture = {
  status: number;
  url: string;
  mode: string;
  /** Base64 of the PNG or PDF. */
  b64: string;
};

export type ScraplingEvalResult = {
  status: number;
  url: string;
  mode: string;
  /** One entry per script in the request, in order. */
  results: unknown[];
};

export type ScraplingRawResult = {
  /** HTTP status of the upstream response (the sidecar itself answered 200). */
  status: number;
  /** Final URL after redirects. */
  url: string;
  body: string;
  size: number;
  mode: ScraplingMode;
};

export class ScraplingError extends Error {}

// Deployments created from the Railway template before the Scrapling service
// existed have no such service, so SCRAPLING_URL resolves to nothing. Those
// stacks must keep working (degraded) rather than break, and they must not pay
// a connection timeout on every single call to find that out.
//
// So the first unreachable-at-the-transport-level failure trips a breaker and
// subsequent calls skip Scrapling entirely until the cooldown expires. Only
// transport failures trip it — an HTTP error means the service is there and
// answering, which is a different problem and shouldn't disable it.
// Kept short. The breaker exists to avoid re-dialling a host that does not
// exist; it is not a load-shedding mechanism, and every minute it stays closed
// is a minute of degraded fetching (no residential egress, no challenge
// solving). A wrong trip must expire fast.
const UNAVAILABLE_COOLDOWN_MS = 60_000;
let unavailableUntil = 0;

/**
 * Does this error mean "there is no such service", as opposed to "the service is
 * busy or slow"?
 *
 * This distinction is the whole safety of the breaker. An earlier version
 * tripped on any failure, including a timeout — so one slow moment took
 * residential egress out for five minutes and LinkedIn silently fell back to
 * the datacenter IP, where it is blocked outright. Only unresolvable/refused
 * hosts count, which is exactly the pre-Scrapling-template case we want to
 * absorb, and those fail in milliseconds.
 */
function isUnreachable(err: unknown): boolean {
  const codes = new Set([
    'ENOTFOUND',
    'ECONNREFUSED',
    'EAI_AGAIN',
    'EHOSTUNREACH',
    'ENETUNREACH',
    'ERR_INVALID_URL',
  ]);
  let cur: unknown = err;
  for (let depth = 0; cur && depth < 5; depth++) {
    const code = (cur as { code?: unknown }).code;
    if (typeof code === 'string' && codes.has(code)) return true;
    cur = (cur as { cause?: unknown }).cause;
  }
  return false;
}

export function scraplingAvailable(): boolean {
  return Date.now() >= unavailableUntil;
}

function markUnavailable(reason: string): void {
  const firstTrip = scraplingAvailable();
  unavailableUntil = Date.now() + UNAVAILABLE_COOLDOWN_MS;
  if (firstTrip) {
    process.stderr.write(
      `[scrapling] unreachable (${reason}); skipping it for the next ` +
        `${UNAVAILABLE_COOLDOWN_MS / 60_000}min. web_fetch/web_html fall back to ` +
        `Camoufox; captures and markdown report the outage until the cooldown expires.\n`,
    );
  }
}

/**
 * POST to the sidecar with the shared breaker + timeout policy.
 *
 * No pre-flight health probe. One was tried and made things worse: it added a
 * round trip to every call, and its short deadline meant a momentarily busy
 * sidecar looked *absent*, tripping the breaker and silently demoting LinkedIn
 * to the datacenter IP. A host that genuinely does not exist fails DNS in
 * milliseconds, so the real request is already a fast enough probe.
 *
 * The caller's own timeoutMs is the sidecar's hard deadline; the client abort
 * sits 25s above it to let the sidecar's honest 504 win the race (its internal
 * slack is 20s — see HARD_DEADLINE_SLACK_S in app.py). Keep 25 > 20.
 */
async function postScrapling<T>(path: string, body: unknown, timeoutMs: number): Promise<T> {
  if (!Config.scrapling.url) {
    throw new ScraplingError('SCRAPLING_URL is not configured');
  }
  if (!scraplingAvailable()) {
    throw new ScraplingError('scrapling marked unavailable; skipping until cooldown expires');
  }

  let response: Response;
  try {
    response = await fetch(new URL(path, Config.scrapling.url), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs + 25_000),
    });
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err);
    // Only a genuinely absent host disables the sidecar. A timeout means it is
    // there and working on something, so let the next call try again.
    if (isUnreachable(err)) markUnavailable(reason);
    throw new ScraplingError(`scrapling ${path} failed: ${reason}`);
  }

  if (!response.ok) {
    const text = await response.text().catch(() => '');
    throw new ScraplingError(`scrapling ${path} HTTP ${response.status}: ${text.slice(0, 300)}`);
  }

  return (await response.json()) as T;
}

export async function scraplingFetch(params: {
  url: string;
  mode?: ScraplingMode;
  timeoutMs?: number;
  networkIdle?: boolean;
  /** Settle time after the page is stable, before the HTML is returned. */
  waitMs?: number;
}): Promise<ScraplingResult> {
  const timeoutMs = params.timeoutMs ?? 60_000;
  return postScrapling<ScraplingResult>('/fetch', {
    url: params.url,
    ...(params.mode ? { mode: params.mode } : {}),
    network_idle: params.networkIdle ?? false,
    timeout_ms: timeoutMs,
    wait_ms: params.waitMs ?? 0,
  }, timeoutMs);
}

/**
 * Plain HTTP GET through the sidecar — no browser, no challenge handling.
 *
 * Callers use this when the egress matters more than the rendering: web.archive.org
 * drops this project's datacenter IPs entirely, so the CDX API and archived pages
 * have to leave on the residential exit (the sidecar picks that by host).
 * Text bodies only; a 4xx/5xx upstream arrives as `status`, not as an error.
 */
export async function scraplingRaw(params: {
  url: string;
  mode?: ScraplingMode;
  timeoutMs?: number;
}): Promise<ScraplingRawResult> {
  const timeoutMs = params.timeoutMs ?? 60_000;
  return postScrapling<ScraplingRawResult>('/raw', {
    url: params.url,
    ...(params.mode ? { mode: params.mode } : {}),
    timeout_ms: timeoutMs,
  }, timeoutMs);
}

/**
 * Render HTML to markdown (the sidecar's /markdown: strip+sanitize, convert,
 * absolutise links). `filter: 'fit'` scopes to <body>; `'raw'` takes the whole
 * document. Both drop scripts and hidden content.
 */
export async function scraplingRenderMarkdown(params: {
  html: string;
  /** The URL the HTML came from; relative links resolve against it. */
  url: string;
  filter?: 'raw' | 'fit';
  cssSelector?: string;
}): Promise<string> {
  const r = await postScrapling<{ markdown: string }>('/markdown', {
    html: params.html,
    url: params.url,
    filter: params.filter ?? 'fit',
    ...(params.cssSelector ? { css_selector: params.cssSelector } : {}),
  }, 60_000);
  return r.markdown;
}

export async function scraplingScreenshot(params: {
  url: string;
  fullPage?: boolean;
  waitMs?: number;
  networkIdle?: boolean;
  timeoutMs?: number;
}): Promise<ScraplingCapture> {
  const timeoutMs = params.timeoutMs ?? 60_000;
  return postScrapling<ScraplingCapture>('/screenshot', {
    url: params.url,
    full_page: params.fullPage ?? true,
    wait_ms: params.waitMs ?? 0,
    network_idle: params.networkIdle ?? false,
    timeout_ms: timeoutMs,
  }, timeoutMs);
}

export async function scraplingPdf(params: {
  url: string;
  waitMs?: number;
  timeoutMs?: number;
  format?: string;
  landscape?: boolean;
}): Promise<ScraplingCapture> {
  const timeoutMs = params.timeoutMs ?? 60_000;
  return postScrapling<ScraplingCapture>('/pdf', {
    url: params.url,
    wait_ms: params.waitMs ?? 0,
    timeout_ms: timeoutMs,
    ...(params.format ? { format: params.format } : {}),
    ...(params.landscape ? { landscape: true } : {}),
  }, timeoutMs);
}

export async function scraplingEval(params: {
  url: string;
  /** JS expressions or IIFEs, evaluated in order on the same page. */
  scripts: string[];
  waitMs?: number;
  timeoutMs?: number;
}): Promise<ScraplingEvalResult> {
  const timeoutMs = params.timeoutMs ?? 60_000;
  return postScrapling<ScraplingEvalResult>('/eval', {
    url: params.url,
    scripts: params.scripts,
    wait_ms: params.waitMs ?? 0,
    timeout_ms: timeoutMs,
  }, timeoutMs);
}
