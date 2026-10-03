import type { z } from 'zod';
import {
  camoufoxBytes,
  camoufoxEval,
  camoufoxFormInspect,
  camoufoxFormSubmit,
  CamoufoxError,
  camoufoxRecycle,
  camoufoxRender,
  camoufoxScreenshot,
  camoufoxSpaFetch,
  camoufoxStealth,
} from './camoufox.js';
import { Config } from './config.js';
import { web_agent } from './agent.js';
import { errMsg, log } from './log.js';
import { renderMarkdown } from './markdown.js';
import { forcedWaitMs, pickBackend, type Backend } from './routing.js';
import type {
  WebArchiveInput,
  WebBytesInput,
  WebCrawlInput,
  WebEvalInput,
  WebExecuteJsInput,
  WebFetchInput,
  WebHtmlInput,
  WebPdfInput,
  WebScreenshotInput,
  WebSearchInput,
  WebSnapshotsInput,
  WebSpaFetchInput,
} from './schemas.js';
import { CRAWL_DEADLINE_MS } from './schemas.js';
import { scraplingEval, scraplingFetch, scraplingPdf, scraplingScreenshot } from './scrapling.js';
import { searchSearXNG } from './searxng.js';
import { SidecarError } from './sidecar.js';
import { getStats, recordCall } from './stats.js';
import type { ToolName, ToolResult } from './types.js';
import { getArchivedPage, getSnapshots } from './wayback.js';

// ── Results ──────────────────────────────────────────────────────────

const text = (t: string, isError = false): ToolResult => ({
  content: [{ type: 'text', text: t }],
  isError,
});
const json = (payload: unknown, isError = false): ToolResult => text(JSON.stringify(payload), isError);
/** A JSON-native tool's result: REST returns `data` bare, MCP the JSON text. */
const data = (payload: unknown): ToolResult => ({
  ...text(JSON.stringify(payload, null, 2)),
  data: payload,
});

// ── Routing with symmetric fallback ──────────────────────────────────

const other = (b: Backend): Backend => (b === 'scrapling' ? 'camoufox' : 'scrapling');

/**
 * Run on the backend the host calls for (routing.ts), then on the other.
 *
 * Both directions fall back: a Camoufox render fails transiently
 * (NS_ERROR_CONNECTION_REFUSED on a healthy origin), Scrapling can be down,
 * and either way the tool should degrade the exit and browser, not die. When
 * both fail the error names both causes — which fired first is the interesting
 * half of a dual-outage diagnosis.
 *
 * Except on a 4xx: the sidecar understood the request and refused it (a bad
 * parameter, a script that threw). Replaying it elsewhere would hide the bug
 * and, for scripts, run their side effects twice.
 */
async function routed<T>(url: string, label: string, run: Record<Backend, () => Promise<T>>): Promise<T> {
  const first = pickBackend(url);
  const second = other(first);
  try {
    return await run[first]();
  } catch (err) {
    if (err instanceof SidecarError && err.status !== undefined && err.status >= 400 && err.status < 500) {
      throw err;
    }
    log(`${label}: ${first} failed, falling back to ${second}:`, errMsg(err));
    try {
      return await run[second]();
    } catch (err2) {
      throw new Error(`${first}: ${errMsg(err)}; ${second}: ${errMsg(err2)}`);
    }
  }
}

// ── Fetching ─────────────────────────────────────────────────────────

/** A fetched page, normalised across the backends that can produce one. */
type FetchedPage = {
  status: number;
  url: string;
  html: string;
  size: number;
  mode: string;
  escalated: boolean;
};

type PageOpts = {
  timeoutMs?: number;
  /** Absolute epoch ms; each attempt's timeout shrinks to fit inside it. */
  deadlineAt?: number;
  waitUntil?: string;
  waitMs?: number;
  clickAll?: string[];
  settleMs?: number;
  freshIp?: boolean;
};

// What a sidecar call may cost above its own timeout before the client aborts
// (camoufox: +30s, scrapling: +25s).
const CLIENT_OVERHEAD_MS = 30_000;

function attemptTimeout(opts: PageOpts): number {
  const timeoutMs = opts.timeoutMs ?? 60_000;
  if (opts.deadlineAt === undefined) return timeoutMs;
  const left = opts.deadlineAt - Date.now() - CLIENT_OVERHEAD_MS;
  if (left < 5_000) throw new Error('deadline exceeded before the fetch could start');
  return Math.min(timeoutMs, left);
}

async function camoufoxPage(url: string, opts: PageOpts): Promise<FetchedPage> {
  // A host that NEEDS a settle time gets it whatever the caller asked for.
  const forced = forcedWaitMs(url);
  const r = await camoufoxRender({
    url,
    timeoutMs: attemptTimeout(opts),
    waitUntil: opts.waitUntil,
    waitMs: forced ?? opts.waitMs,
    clickAll: opts.clickAll,
    settleMs: opts.settleMs,
    freshIp: opts.freshIp,
  });
  return { status: r.status, url: r.url, html: r.html, size: r.html.length, mode: 'camoufox', escalated: false };
}

async function scraplingPage(url: string, opts: PageOpts): Promise<FetchedPage> {
  // No mode passed: the sidecar routes by host and escalates to a challenge
  // solve only on evidence (and never for hosts it cannot clear).
  return scraplingFetch({
    url,
    timeoutMs: attemptTimeout(opts),
    networkIdle: opts.waitUntil === 'networkidle',
    waitMs: opts.waitMs,
  });
}

/** Every page consumer (web_fetch, web_html, web_crawl) fetches through here. */
function fetchPage(url: string, opts: PageOpts = {}): Promise<FetchedPage> {
  return routed(url, 'fetchPage', {
    camoufox: () => camoufoxPage(url, opts),
    scrapling: () => scraplingPage(url, opts),
  });
}

// ── Tool implementations ─────────────────────────────────────────────
// Each returns a ToolResult or throws; `instrument` (below) counts both and
// turns a throw into an isError result, so error handling lives in one place.

export async function web_search(params: z.infer<typeof WebSearchInput>): Promise<ToolResult> {
  return data(await searchSearXNG(params.query, { limit: params.limit ?? 10, engines: params.engines }));
}

export async function web_fetch(params: z.infer<typeof WebFetchInput>): Promise<ToolResult> {
  // An explicit delay is a settle time the caller asked for; absent it, the
  // fetcher's own stability wait is the whole story (no hidden default).
  const delayMs = params.delay && params.delay > 0 ? Math.round(params.delay * 1000) : undefined;
  const page = await fetchPage(params.url, { timeoutMs: 60_000, waitMs: delayMs });
  const provenance = `mode=${page.mode}${page.escalated ? ', escalated' : ''}`;
  // The URL we landed on (after redirects), so relative links resolve right.
  const markdown = page.html
    ? (await renderMarkdown({ html: page.html, url: page.url || params.url, filter: params.f ?? 'fit' })).markdown
    : '';
  if (!markdown) {
    return text(`web_fetch: upstream returned HTTP ${page.status} with no extractable content (${page.size} bytes, ${provenance}).`, true);
  }
  // A challenge page converts to markdown perfectly well, so the HTTP status is
  // the honest signal. Keep the body either way: it makes the wall diagnosable.
  if (page.status >= 400) {
    return text(`web_fetch: upstream returned HTTP ${page.status} (${provenance}). Body as markdown follows.\n\n${markdown}`, true);
  }
  return text(markdown);
}

/**
 * Raw HTML, deliberately not markdown: JSON-LD, meta tags and attributes are
 * what structured scrapers need (gtm-tools parses the LinkedIn JSON-LD
 * `Person`). A non-2xx is reported in `status`, not as an error — callers
 * branch on 999 vs 404 themselves.
 */
export async function web_html(params: z.infer<typeof WebHtmlInput>): Promise<ToolResult> {
  const page = await fetchPage(params.url, {
    timeoutMs: params.timeout_ms ?? 60_000,
    waitUntil: params.wait_until ?? (params.network_idle ? 'networkidle' : undefined),
    waitMs: params.wait_ms,
    clickAll: params.click_all,
    settleMs: params.settle_ms,
    freshIp: params.fresh_ip === true,
  });
  return json({
    status: page.status,
    url: page.url,
    mode: page.mode,
    escalated: page.escalated,
    size: page.size,
    html: page.html,
  });
}

/**
 * Sequential, one fetch + one render per URL, in the caller's order. Both
 * sidecars serialise per browser mode anyway, so concurrency would only
 * shuffle the queue. A failed or skipped URL is reported in its own slot.
 */
export async function web_crawl(params: z.infer<typeof WebCrawlInput>): Promise<ToolResult> {
  const deadlineAt = Date.now() + CRAWL_DEADLINE_MS;
  const results: Array<Record<string, unknown>> = [];
  for (const url of params.urls) {
    try {
      const page = await fetchPage(url, { timeoutMs: params.timeout_ms ?? 60_000, deadlineAt });
      const { markdown, renderer } = page.html
        ? await renderMarkdown({ html: page.html, url: page.url || url, filter: 'fit', cssSelector: params.css_selector })
        : { markdown: '', renderer: undefined };
      results.push({
        url,
        status_code: page.status,
        success: page.status < 400 && !!markdown,
        mode: page.mode,
        ...(renderer ? { renderer } : {}),
        markdown,
      });
    } catch (err) {
      log(`web_crawl: ${url} failed:`, errMsg(err));
      results.push({ url, status_code: 0, success: false, error: errMsg(err) });
    }
  }
  return json({ results }, results.every((r) => r.success !== true));
}

// ── Captures ─────────────────────────────────────────────────────────
// Capturing what the routed visitor sees is the point, so captures route like
// fetches — with the other backend as fallback.

export async function web_screenshot(params: z.infer<typeof WebScreenshotInput>): Promise<ToolResult> {
  const { url } = params;
  const waitMs = params.screenshot_wait_for !== undefined ? Math.round(params.screenshot_wait_for * 1000) : 2000;
  const shot = await routed(url, 'web_screenshot', {
    scrapling: () => scraplingScreenshot({ url, waitMs }),
    camoufox: () => camoufoxScreenshot({ url, waitMs }),
  });
  return { content: [{ type: 'image', data: shot.b64, mimeType: 'image/png' }], isError: shot.status >= 400 };
}

/**
 * Print-to-PDF is Chromium-only, so Scrapling always prints. For a host that
 * routes to Camoufox, Camoufox renders the page as the right visitor and
 * Scrapling prints that DOM (scripts stripped; subresources load from
 * Scrapling's egress).
 */
export async function web_pdf(params: z.infer<typeof WebPdfInput>): Promise<ToolResult> {
  const { url } = params;
  const pdf = await routed(url, 'web_pdf', {
    scrapling: () => scraplingPdf({ url, timeoutMs: 60_000 }),
    camoufox: async () => {
      const page = await camoufoxPage(url, { timeoutMs: 60_000 });
      const printed = await scraplingPdf({ url: page.url, html: page.html, timeoutMs: 60_000 });
      return { ...printed, status: page.status };
    },
  });
  return {
    content: [{ type: 'resource', resource: { uri: url, mimeType: 'application/pdf', blob: pdf.b64 } }],
    isError: pdf.status >= 400,
  };
}

/**
 * Camoufox evaluates one expression per page, so the scripts are sequenced in
 * one: each is evaluated in order (a function is called, a promise awaited)
 * and the results collected — the same contract as Scrapling's /eval.
 */
function sequenceScripts(scripts: string[]): string {
  return `(async () => {
  const out = [];
  for (const src of ${JSON.stringify(scripts)}) {
    let v = (0, eval)(src);
    if (typeof v === 'function') v = v();
    out.push(await v);
  }
  return out;
})()`;
}

export async function web_execute_js(params: z.infer<typeof WebExecuteJsInput>): Promise<ToolResult> {
  const { url, scripts } = params;
  const r = await routed(url, 'web_execute_js', {
    scrapling: () => scraplingEval({ url, scripts, timeoutMs: 60_000 }),
    camoufox: async () => {
      const e = await camoufoxEval({ url, js: sequenceScripts(scripts), timeoutMs: 60_000 });
      return { status: e.status, url: e.url, mode: 'camoufox', results: Array.isArray(e.result) ? e.result : [e.result] };
    },
  });
  return json({ status: r.status, url: r.url, mode: r.mode, results: r.results });
}

// ── Wayback ──────────────────────────────────────────────────────────

export async function web_snapshots(params: z.infer<typeof WebSnapshotsInput>): Promise<ToolResult> {
  return data(
    await getSnapshots({
      url: params.url,
      from: params.from,
      to: params.to,
      limit: params.limit,
      matchType: params.match_type,
      filter: params.filter,
    }),
  );
}

const ARCHIVE_MAX_LENGTH = 50_000;

export async function web_archive(params: z.infer<typeof WebArchiveInput>): Promise<ToolResult> {
  const { waybackUrl, content } = await getArchivedPage(params);
  const truncated = content.length > ARCHIVE_MAX_LENGTH;
  return data({
    waybackUrl,
    contentLength: content.length,
    content: truncated ? content.substring(0, ARCHIVE_MAX_LENGTH) + '\n\n[Content truncated]' : content,
  });
}

/** Process-local cost/usage counters. See stats.ts. */
export async function web_usage_stats(_params: Record<string, unknown>): Promise<ToolResult> {
  return data(getStats());
}

// ── Camoufox-only tools ──────────────────────────────────────────────
// What the Italian residential Firefox can do and Scrapling cannot: a binary
// download, a warmed-session POST, forms. Separate tools, not flags, because
// the capability differs, not just the egress.

/** Raw bytes through the residential exit, base64 (PDFs behind bot-gated origins). */
export async function web_bytes(params: z.infer<typeof WebBytesInput>): Promise<ToolResult> {
  const r = await camoufoxBytes({ url: params.url, timeoutMs: params.timeout_ms });
  // A non-2xx is reported in `status`, not raised — a 404 PDF is an answer.
  return json({ status: r.status, url: params.url, size_b64: r.b64.length, b64: r.b64 });
}

/**
 * The structured failure of a form call, so an agent never parses a string to
 * learn whether it may replay:
 *   503 + detail.retryable → {retryable:true, form_submissions:0}: provably
 *       nothing was sent; the same identity may be replayed (bounded).
 *   400/422 → invalid_request: rejected before any browser ran.
 *   anything else (502, lost response, timeout) → outcome 'unknown': a POST
 *       may have left; never replay.
 */
function failureReason(err: unknown): string {
  const detail = err instanceof CamoufoxError ? err.detail : undefined;
  if (detail && typeof detail === 'object' && 'message' in detail) {
    return String((detail as { message: unknown }).message);
  }
  return typeof detail === 'string' ? detail : errMsg(err);
}

function formFailure(err: unknown): Record<string, unknown> {
  const status = err instanceof CamoufoxError ? err.status : undefined;
  const detail = err instanceof CamoufoxError ? err.detail : undefined;
  const reason = failureReason(err);
  if (status === 503 && detail && typeof detail === 'object' && (detail as { retryable?: unknown }).retryable === true) {
    // A named zero-POST failure (e.g. no_scoring_exit) keeps its code and
    // the score gate's record; otherwise the message is the error.
    // With retry_on_captcha_rejection, every attempt was zero-POST (e.g.
    // captcha_token_missing on each exit): `attempts` names them.
    // exit_mismatch: the token left the IP the gate scored (gate_ip vs form_ip).
    const d = detail as { error?: unknown; score_gate?: unknown; attempts?: unknown; exit_mismatches?: unknown };
    return {
      ok: false, retryable: true, outcome: 'not_submitted', form_submissions: 0, status,
      error: typeof d.error === 'string' ? d.error : reason,
      ...(d.score_gate !== undefined ? { score_gate: d.score_gate } : {}),
      ...(Array.isArray(d.attempts) ? { attempts: d.attempts } : {}),
      ...(Array.isArray(d.exit_mismatches) ? { exit_mismatches: d.exit_mismatches } : {}),
    };
  }
  if (status === 400 || status === 422) {
    return {
      ok: false,
      retryable: false,
      outcome: 'invalid_request',
      form_submissions: 0,
      status,
      error: status === 422 ? JSON.stringify(detail ?? reason).slice(0, 500) : reason,
    };
  }
  // A retry whose outcome is unknown (retry_on_captcha_rejection): the
  // earlier attempts' POSTs are known and reported; the total is not.
  const retried = detail && typeof detail === 'object' ? (detail as { attempts?: unknown; form_submissions_before?: unknown }) : undefined;
  return {
    ok: false, retryable: false, outcome: 'unknown', form_submissions: null, status: status ?? 0, error: reason,
    ...(Array.isArray(retried?.attempts) ? { attempts: retried.attempts, form_submissions_before: retried.form_submissions_before } : {}),
  };
}

function formResult(tool: 'web_form_submit' | 'web_form_inspect', body: Record<string, unknown>, isError: boolean): ToolResult {
  return { content: [{ type: 'text', text: JSON.stringify(body) }], isError };
}

/**
 * Fill and submit a form in the residential Firefox.
 *
 * web_execute_js runs scripts from Scrapling; this is the same idea driven
 * from an Italian residential Firefox, for forms on sites that bot-gate this
 * host's own IP — and for the POST itself, which needs a warmed session on
 * Akamai-gated origins.
 */
export async function web_form_submit(params: Record<string, unknown>): Promise<ToolResult> {
  const url = params.url as string | undefined;
  const submit = params.submit as string | undefined;
  const fields = params.fields as Array<Record<string, unknown>> | undefined;
  if (!url || !submit || !Array.isArray(fields)) {
    return formResult('web_form_submit', {
      ok: false,
      retryable: false,
      outcome: 'invalid_request',
      form_submissions: 0,
      error: '`url`, `fields` and `submit` are required',
    }, true);
  }
  try {
    const r = await camoufoxFormSubmit({
      url,
      submit,
      fields: fields as never,
      dismiss: params.dismiss as string[] | undefined,
      successUrl: params.success_url as string | undefined,
      submissionUrls: params.submission_urls as string[] | undefined,
      captchaField: params.captcha_field as string | undefined,
      requireCaptchaToken: params.require_captcha_token === true,
      readyExpression: params.ready_expression as string | undefined,
      inspectOnly: params.inspect_only === true,
      waitUntil: params.wait_until as string | undefined,
      waitMs: typeof params.wait_ms === 'number' ? params.wait_ms : undefined,
      settleMs: typeof params.settle_ms === 'number' ? params.settle_ms : undefined,
      timeoutMs: typeof params.timeout_ms === 'number' ? params.timeout_ms : undefined,
      freshIp: params.fresh_ip !== false,
      exitSession: params.exit_session as string | undefined,
      headed: params.headed === true,
      gateText: params.gate_text as string | undefined,
      step2: params.step2 as never,
      step2Submit: params.step2_submit as string | undefined,
      completionMarkers: params.completion_markers as string[] | undefined,
      profile: params.profile as string | undefined,
      stopAfterPosts: typeof params.stop_after_posts === 'number' ? params.stop_after_posts : undefined,
      stickyExit: typeof params.sticky_exit === 'boolean' ? params.sticky_exit : undefined,
      scoreGate: typeof params.score_gate === 'boolean' ? params.score_gate : undefined,
      scoreThreshold: typeof params.score_threshold === 'number' ? params.score_threshold : undefined,
      scoreGateTries: typeof params.score_gate_tries === 'number' ? params.score_gate_tries : undefined,
      // The gate probes OUR oracle; the sidecar only gates when it has one.
      oracleUrl: params.score_gate === false || params.inspect_only === true ? undefined : (Config.oracleUrl ?? undefined),
      retryOnCaptchaRejection: typeof params.retry_on_captcha_rejection === 'number' ? params.retry_on_captcha_rejection : undefined,
      captchaRejectionText: typeof params.captcha_rejection_text === 'string' ? params.captcha_rejection_text : undefined,
      captchaLibDirect: typeof params.captcha_lib_direct === 'boolean' ? params.captcha_lib_direct : undefined,
    });
    // An answered run is never auto-replayable, whatever its outcome: the
    // only sanctioned replay is the 503 below.
    // The sidecar's model always carries `attempts` (null unless retrying):
    // a single-attempt answer keeps its v2 shape.
    const { attempts, ...answer } = r;
    return formResult('web_form_submit', { ...answer, ...(attempts ? { attempts } : {}), retryable: false }, false);
  } catch (err) {
    const failure = formFailure(err);
    log('web_form_submit failed:', failure.outcome, failure.status);
    return formResult('web_form_submit', failure, true);
  }
}

/**
 * Read-only: describe a page's forms so an agent can build web_form_submit's
 * `fields[]` without hand-writing selectors. Same browser path as the submit;
 * nothing is filled or clicked and every mutating request is aborted, so any
 * failure is safe to retry.
 */
export async function web_form_inspect(params: Record<string, unknown>): Promise<ToolResult> {
  const url = params.url as string | undefined;
  if (!url) {
    return formResult('web_form_inspect', { ok: false, retryable: false, error: '`url` is required' }, true);
  }
  try {
    const r = await camoufoxFormInspect({
      url,
      waitUntil: params.wait_until as string | undefined,
      waitMs: typeof params.wait_ms === 'number' ? params.wait_ms : undefined,
      timeoutMs: typeof params.timeout_ms === 'number' ? params.timeout_ms : undefined,
      freshIp: params.fresh_ip !== false,
      exitSession: params.exit_session as string | undefined,
      headed: params.headed === true,
      profile: params.profile as string | undefined,
    });
    return formResult('web_form_inspect', r, false);
  } catch (err) {
    const status = err instanceof CamoufoxError ? err.status : undefined;
    const invalid = status === 400 || status === 422;
    log('web_form_inspect failed:', errMsg(err));
    return formResult('web_form_inspect', {
      ok: false,
      // Read-only: nothing can have been submitted, so only a bad request
      // is not worth repeating.
      retryable: !invalid,
      form_submissions: 0,
      status: status ?? 0,
      error: failureReason(err),
    }, true);
  }
}

export async function web_eval(params: z.infer<typeof WebEvalInput>): Promise<ToolResult> {
  const r = await camoufoxEval({
    url: params.url,
    js: params.js,
    waitUntil: params.wait_until,
    waitMs: params.wait_ms,
    timeoutMs: params.timeout_ms,
    freshIp: params.fresh_ip === true,
  });
  return json({ status: r.status, url: r.url, result: r.result });
}

/**
 * Same-origin in-page fetch on a warmed page, for origins that gate POSTs on an
 * Akamai sensor cookie. Stateful, and the only tool here that is: the sidecar
 * keeps one warmed page per (base_url, warm_path) on a sticky exit and feeds
 * the sensor so `_abck` stays validated. web_recycle, or anything that tears
 * the browser down, costs whoever is mid-crawl their maturation. The upstream
 * status is data: 403 means the sensor has not cleared (re-mature or recycle).
 */
export async function web_spa_fetch(params: z.infer<typeof WebSpaFetchInput>): Promise<ToolResult> {
  const r = await camoufoxSpaFetch({
    baseUrl: params.base_url,
    path: params.path,
    warmPath: params.warm_path,
    method: params.method,
    body: params.body,
    accept: params.accept,
    sensorWaitMs: params.sensor_wait_ms,
    maturProbe: params.mature_probe,
    maturMaxTries: params.mature_max_tries,
    timeoutMs: params.timeout_ms,
  });
  return json({ status: r.status, text: r.text });
}

/**
 * Drop the warmed session and the render browser, and mint a fresh exit IP.
 * Expensive (~30-60s) and destructive to anyone mid-crawl; for a fresh IP on
 * one request, pass fresh_ip to web_eval instead (~1s).
 */
export async function web_recycle(_params: Record<string, unknown>): Promise<ToolResult> {
  return json(await camoufoxRecycle());
}

// ── Instrumentation + function map ───────────────────────────────────

// Upstream failure modes worth counting as errors even when handed back as
// content: challenge pages (web_html reports status as data), explicit
// anti-bot signals, and the sidecar's own hard deadline / capture failures.
const BLOCK_RE =
  /HTTP 429|Too Many Requests|Cloudflare JS challenge|Just a moment\.\.\.|upstream returned HTTP [45]|hard deadline|capture failed/i;

function payloadBytes(result: ToolResult): number {
  return result.content.reduce(
    (n, c) => n + (c.type === 'text' ? c.text.length : c.type === 'image' ? c.data.length : c.resource.blob.length),
    0,
  );
}

function instrument(tool: ToolName, impl: (params: any) => Promise<ToolResult>) {
  return async (params: any): Promise<ToolResult> => {
    try {
      const result = await impl(params);
      const blocked = result.content.some((c) => c.type === 'text' && BLOCK_RE.test(c.text));
      recordCall(tool, payloadBytes(result), !!result.isError || blocked);
      return result;
    } catch (err) {
      const msg = errMsg(err);
      log(`${tool} failed:`, msg);
      recordCall(tool, 0, true);
      return text(`${tool} error: ${msg}`, true);
    }
  };
}

// Stealth-score diagnostics (REST-only): the FORM browser's reCAPTCHA v3 score
// against OUR key (packages/api/src/oracle.ts) — the only score to tune on.
function stealth(path: string, needsOracle: boolean) {
  return async (params: Record<string, unknown>): Promise<ToolResult> => {
    const body: Record<string, unknown> = { ...params };
    const oracle = Config.oracleUrl;
    if (needsOracle && !body.oracle_url && oracle) body.oracle_url = oracle;
    if (path === '/form-warm' && !body.target_url && oracle) body.target_url = new URL(oracle).origin + '/';
    if (needsOracle && !body.oracle_url) {
      return { content: [{ type: 'text', text: 'oracle URL unknown (set RECAPTCHA_ORACLE_URL or pass oracle_url)' }], isError: true };
    }
    return { content: [{ type: 'text', text: JSON.stringify(await camoufoxStealth(path, body)) }], isError: false };
  };
}

const impls: Record<ToolName, (params: any) => Promise<ToolResult>> = {
  web_search,
  web_fetch,
  web_html,
  web_screenshot,
  web_pdf,
  web_execute_js,
  web_crawl,
  web_snapshots,
  web_archive,
  web_usage_stats,
  web_bytes,
  web_eval,
  web_form_submit,
  web_form_inspect,
  web_spa_fetch,
  web_recycle,
  web_agent,
  web_form_score_probe: stealth('/form-score-probe', true),
  web_form_warm: stealth('/form-warm', false),
  web_form_exit_select: stealth('/form-exit-select', true),
  web_form_exits: stealth('/form-exits', false),
};

/** Every tool, instrumented: counted in /stats, never throws. Inputs must be validated. */
export const functionMap = Object.fromEntries(
  Object.entries(impls).map(([name, impl]) => [name, instrument(name as ToolName, impl)]),
) as Record<ToolName, (params: any) => Promise<ToolResult>>;
