// Client for the Scrapling sidecar (services/scrapling): Patchright Chromium on
// a US residential exit (stealth) or this host's own IP (fast), with
// JS-challenge solving.
//
//   /fetch       pages: residential egress, JS-challenge solving
//   /markdown    HTML→markdown, pure CPU (no browser)
//   /raw         plain HTTP GET (CDX JSON, archived pages) — no browser
//   /screenshot  /pdf /eval through one shared browser session per mode

import { Config } from './config.js';
import { log } from './log.js';
import { createSidecar, SidecarError } from './sidecar.js';

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

export class ScraplingError extends SidecarError {}

/**
 * The sidecar's hard cap on any browser run (MAX_FETCH_MS in app.py). Schemas
 * reject anything above it rather than letting the sidecar cap it silently.
 */
export const SCRAPLING_MAX_TIMEOUT_MS = 90_000;

// The client abort sits 25s above the caller's timeout so the sidecar's honest
// 504 wins the race: its own deadline is timeout + HARD_DEADLINE_SLACK_S (20s)
// for the whole request, escalation included. Keep 25 > 20.
const CLIENT_SLACK_MS = 25_000;

export const scrapling = createSidecar({
  name: 'scrapling',
  url: () => Config.scrapling.url,
  error: (m, s, d) => new ScraplingError(m, s, d),
  onTrip: (reason) =>
    log(
      `[scrapling] unreachable (${reason}); skipping it for 60s. Fetches and captures ` +
        'fall back to Camoufox, markdown renders locally.',
    ),
});

const post = <T>(path: string, body: unknown, timeoutMs: number) =>
  scrapling.post<T>(path, body, timeoutMs + CLIENT_SLACK_MS);

export function scraplingFetch(params: {
  url: string;
  mode?: ScraplingMode;
  timeoutMs?: number;
  networkIdle?: boolean;
  /** Settle time after the page is stable, before the HTML is returned. */
  waitMs?: number;
}): Promise<ScraplingResult> {
  const timeoutMs = params.timeoutMs ?? 60_000;
  return post<ScraplingResult>('/fetch', {
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
 * For when the egress matters more than rendering: web.archive.org drops this
 * project's datacenter IPs, so CDX and archived pages leave on the residential
 * exit (the sidecar picks it by host). A 4xx/5xx upstream arrives as `status`.
 */
export function scraplingRaw(params: {
  url: string;
  mode?: ScraplingMode;
  timeoutMs?: number;
}): Promise<ScraplingRawResult> {
  const timeoutMs = params.timeoutMs ?? 60_000;
  return post<ScraplingRawResult>('/raw', {
    url: params.url,
    ...(params.mode ? { mode: params.mode } : {}),
    timeout_ms: timeoutMs,
  }, timeoutMs);
}

/**
 * Render HTML to markdown (strip+sanitize, convert, absolutise links).
 * `filter: 'fit'` scopes to <body>; `'raw'` takes the whole document.
 * Callers go through markdown.ts, which falls back to a local renderer.
 */
export async function scraplingRenderMarkdown(params: {
  html: string;
  /** The URL the HTML came from; relative links resolve against it. */
  url: string;
  filter?: 'raw' | 'fit';
  cssSelector?: string;
}): Promise<string> {
  const r = await scrapling.post<{ markdown: string }>('/markdown', {
    html: params.html,
    url: params.url,
    filter: params.filter ?? 'fit',
    ...(params.cssSelector ? { css_selector: params.cssSelector } : {}),
  }, 30_000);
  return r.markdown;
}

export function scraplingScreenshot(params: {
  url: string;
  waitMs?: number;
  timeoutMs?: number;
}): Promise<ScraplingCapture> {
  const timeoutMs = params.timeoutMs ?? 60_000;
  return post<ScraplingCapture>('/screenshot', {
    url: params.url,
    wait_ms: params.waitMs ?? 0,
    timeout_ms: timeoutMs,
  }, timeoutMs);
}

export function scraplingPdf(params: {
  url: string;
  /**
   * Print this document instead of fetching `url` live: the navigation to
   * `url` is answered with it (subresources still load). How a page rendered
   * by the right visitor (Camoufox) gets printed by the only engine that can.
   */
  html?: string;
  waitMs?: number;
  timeoutMs?: number;
}): Promise<ScraplingCapture> {
  const timeoutMs = params.timeoutMs ?? 60_000;
  return post<ScraplingCapture>('/pdf', {
    url: params.url,
    ...(params.html ? { html: params.html } : {}),
    wait_ms: params.waitMs ?? 0,
    timeout_ms: timeoutMs,
  }, timeoutMs);
}

export function scraplingEval(params: {
  url: string;
  /** JS expressions or IIFEs, evaluated in order on the same page. */
  scripts: string[];
  waitMs?: number;
  timeoutMs?: number;
}): Promise<ScraplingEvalResult> {
  const timeoutMs = params.timeoutMs ?? 60_000;
  return post<ScraplingEvalResult>('/eval', {
    url: params.url,
    scripts: params.scripts,
    wait_ms: params.waitMs ?? 0,
    timeout_ms: timeoutMs,
  }, timeoutMs);
}
