import {
  camoufoxBytes,
  camoufoxEval,
  camoufoxRecycle,
  camoufoxRender,
  camoufoxScreenshot,
  camoufoxSpaFetch,
  camoufoxFormSubmit,
  camoufoxFormInspect,
  CamoufoxError,
} from './camoufox.js';
import { forcedWaitMs, isItalianSource, prefersCamoufox } from './routing.js';
import {
  scraplingEval,
  scraplingFetch,
  scraplingPdf,
  scraplingRenderMarkdown,
  scraplingScreenshot,
} from './scrapling.js';
import { searchSearXNG } from './searxng.js';
import { web_agent } from './agent.js';
import { getStats, recordCall, type ToolName } from './stats.js';
import { getArchivedPage, getSnapshots } from './wayback.js';
import type { ToolResult } from './types.js';

// Upstream failure modes worth counting as errors in /stats even when the tool
// hands them back as content: challenge pages (web_html reports status as data
// with isError:false), explicit anti-bot signals, and the sidecar's own hard
// deadline / capture failures.
const BLOCK_RE =
  /HTTP 429|Too Many Requests|Cloudflare JS challenge|Just a moment\.\.\.|upstream returned HTTP [45]|hard deadline|capture failed/i;

// Count a tool invocation: bytes = size of the text payload we hand back to
// the caller. There is deliberately no browser-rotation hook here: killing a
// browser cannot change an egress IP the service does not control (the one
// rotation that matters — a vendor exit refusing us — lives in the sidecars,
// where the proxy connection is owned).
function trace(tool: ToolName, result: ToolResult): ToolResult {
  const text = result.content?.[0]?.text ?? '';
  const blocked = BLOCK_RE.test(text);
  recordCall(tool, text.length, !!result.isError || blocked);
  return result;
}
function traceJson(tool: ToolName, payload: unknown): void {
  recordCall(tool, JSON.stringify(payload).length, false);
}

const log = (...args: unknown[]) => {
  process.stderr.write(
    args.map((a) => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ') + '\n',
  );
};

const errMsg = (err: unknown): string => (err instanceof Error ? err.message : String(err));

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

/**
 * Fetch a page through the backend the host calls for.
 *
 * Every consumer (web_fetch, web_html, web_crawl) comes here, so the routing
 * decision exists once: Italian sources and the hosts in routing.ts that the
 * Scrapling solver cannot clear go to Camoufox; everything else to Scrapling.
 * When Scrapling fails — absent service, transport error, a wall only a
 * different exit clears — Camoufox is the symmetric fallback, so an outage
 * degrades the exit and the browser, not the tool.
 */
type PageOpts = {
  timeoutMs?: number;
  waitUntil?: string;
  waitMs?: number;
  clickAll?: string[];
  settleMs?: number;
  freshIp?: boolean;
};

async function fetchPage(url: string, opts: PageOpts = {}): Promise<FetchedPage> {
  const timeoutMs = opts.timeoutMs ?? 60_000;
  // A host that NEEDS a settle time gets it regardless of what the caller asked
  // for (measured minimum — see routing.ts).
  const forced = forcedWaitMs(url);

  const viaCamoufox = async (): Promise<FetchedPage> => {
    const r = await camoufoxRender({
      url,
      timeoutMs,
      ...opts,
      ...(forced !== undefined ? { waitMs: forced } : {}),
    });
    return {
      status: r.status,
      url: r.url,
      html: r.html,
      size: r.html.length,
      mode: 'camoufox',
      escalated: false,
    };
  };

  const viaScrapling = async (): Promise<FetchedPage> => {
    // No mode passed: the sidecar routes by host and escalates to a challenge
    // solve only on evidence (and never for hosts it cannot clear).
    const page = await scraplingFetch({
      url,
      timeoutMs,
      networkIdle: opts.waitUntil === 'networkidle',
      waitMs: opts.waitMs,
    });
    return { ...page };
  };

  const camoufoxFirst = isItalianSource(url) || prefersCamoufox(url);
  const preferred = camoufoxFirst ? viaCamoufox : viaScrapling;
  const fallback = camoufoxFirst ? viaScrapling : viaCamoufox;
  const preferredName = camoufoxFirst ? 'camoufox' : 'scrapling';
  const fallbackName = camoufoxFirst ? 'scrapling' : 'camoufox';

  // Both directions fall back: a Camoufox outage (its renders do fail
  // transiently — NS_ERROR_CONNECTION_REFUSED on an otherwise healthy origin)
  // must not kill the tool when Scrapling can still fetch, and vice versa.
  // What changes is which exit and browser you get, not whether you get a page.
  try {
    return await preferred();
  } catch (err) {
    const preferredMsg = errMsg(err);
    log(`fetchPage: ${preferredName} failed, falling back to ${fallbackName}:`, preferredMsg);
    try {
      return await fallback();
    } catch (err2) {
      // Both backends failed. Report both causes: which one fired first is the
      // interesting half of a dual-outage diagnosis.
      throw new Error(`${preferredName}: ${preferredMsg}; ${fallbackName}: ${errMsg(err2)}`);
    }
  }
}

/** The page-behaviour knobs, read off a tool's params. */
function pageOptsFrom(params: Record<string, unknown>, timeoutMs?: number): PageOpts {
  return {
    timeoutMs,
    waitUntil:
      (params.wait_until as string | undefined) ??
      (params.network_idle === true ? 'networkidle' : undefined),
    waitMs: typeof params.wait_ms === 'number' ? params.wait_ms : undefined,
    clickAll: Array.isArray(params.click_all) ? (params.click_all as string[]) : undefined,
    settleMs: typeof params.settle_ms === 'number' ? params.settle_ms : undefined,
    freshIp: params.fresh_ip === true,
  };
}

// ── Tool handler functions ───────────────────────────────────────────

export async function web_search(params: {
  query: string;
  limit?: number;
  engines?: string;
}) {
  const results = await searchSearXNG(params.query, {
    limit: params.limit ?? 10,
    engines: params.engines,
  });
  traceJson('web_search', results.data);
  return results.data;
}

export async function web_fetch(params: Record<string, unknown>): Promise<ToolResult> {
  const url = params.url as string | undefined;
  if (!url) {
    return {
      content: [{ type: 'text', text: 'web_fetch error: missing required `url`' }],
      isError: true,
    };
  }
  const filter = params.f === 'raw' ? 'raw' : 'fit';
  // An explicit delay is a settle time the caller asked for; absent it, the
  // fetcher's own stability wait is the whole story (no hidden default 2s).
  const delayMs =
    typeof params.delay === 'number' && Number.isFinite(params.delay) && params.delay > 0
      ? Math.round(params.delay * 1000)
      : undefined;

  let page: FetchedPage;
  try {
    page = await fetchPage(url, { timeoutMs: 60_000, waitMs: delayMs });
  } catch (err) {
    // Both backends failed — no third engine to try, so say what happened.
    return trace('web_fetch', {
      content: [{ type: 'text', text: `web_fetch error: ${errMsg(err)}` }],
      isError: true,
    });
  }

  let md: string | null = null;
  if (page.html) {
    try {
      // Pass the URL we actually landed on (after redirects) so relative links
      // resolve against the right origin.
      md = await scraplingRenderMarkdown({
        html: page.html,
        url: page.url || url,
        filter,
      });
    } catch (err) {
      return trace('web_fetch', {
        content: [
          { type: 'text', text: `web_fetch error: could not render markdown: ${errMsg(err)}` },
        ],
        isError: true,
      });
    }
  }

  let result: ToolResult;
  if (md) {
    // A block/challenge page converts to markdown perfectly well, so the
    // HTTP status is the only honest signal here — not whether we got text.
    // Keep the body either way: callers can often still use it, and it makes
    // "which wall did we hit" diagnosable.
    const blocked = page.status >= 400;
    const provenance = `mode=${page.mode}${page.escalated ? ', escalated' : ''}`;
    result = blocked
      ? {
          content: [
            {
              type: 'text',
              text: `web_fetch: upstream returned HTTP ${page.status} (${provenance}). Body as markdown follows.\n\n${md}`,
            },
          ],
          isError: true,
        }
      : { content: [{ type: 'text', text: md }], isError: false };
  } else {
    result = {
      content: [
        {
          type: 'text',
          text: `web_fetch: upstream returned HTTP ${page.status} with no extractable content (${page.size} bytes, mode=${page.mode}).`,
        },
      ],
      isError: true,
    };
  }

  return trace('web_fetch', result);
}

/**
 * Raw HTML, deliberately not markdown.
 *
 * web_fetch's markdown conversion destroys exactly the things structured
 * scrapers need: <script type="application/ld+json"> blocks, meta tags and
 * attributes. gtm-tools' LinkedIn enrichment parses the JSON-LD `Person` out of
 * the page, so it needs the document as served. Returns a JSON envelope so
 * callers can distinguish "fetched, but the origin said 999" from "fetched
 * fine" without guessing from the body.
 */
export async function web_html(params: Record<string, unknown>): Promise<ToolResult> {
  const url = params.url as string | undefined;
  if (!url) {
    return {
      content: [{ type: 'text', text: 'web_html error: missing required `url`' }],
      isError: true,
    };
  }
  const timeoutMs =
    typeof params.timeout_ms === 'number' && Number.isFinite(params.timeout_ms)
      ? params.timeout_ms
      : 60_000;

  let page: FetchedPage;
  try {
    page = await fetchPage(url, pageOptsFrom(params, timeoutMs));
  } catch (err) {
    return trace('web_html', {
      content: [{ type: 'text', text: `web_html error: ${errMsg(err)}` }],
      isError: true,
    });
  }

  const result: ToolResult = {
    content: [
      {
        type: 'text',
        text: JSON.stringify({
          status: page.status,
          url: page.url,
          mode: page.mode,
          escalated: page.escalated,
          size: page.size,
          html: page.html,
        }),
      },
    ],
    // A non-2xx is reported in `status` rather than as a tool error: callers
    // like the LinkedIn path branch on 999 vs 404 themselves, and losing the
    // body would take that decision away from them.
    isError: false,
  };
  return trace('web_html', result);
}

// ── Captures ─────────────────────────────────────────────────────────

export async function web_screenshot(params: Record<string, unknown>): Promise<ToolResult> {
  const url = params.url as string | undefined;
  if (!url) {
    return {
      content: [{ type: 'text', text: 'web_screenshot error: missing required `url`' }],
      isError: true,
    };
  }
  const waitMs =
    typeof params.screenshot_wait_for === 'number'
      ? Math.round(params.screenshot_wait_for * 1000)
      : 2000;
  const fullPage = params.full_page !== false;

  const viaCamoufox = async (): Promise<ToolResult> => {
    const r = await camoufoxScreenshot({ url, fullPage, waitMs });
    return trace('web_screenshot', {
      content: [{ type: 'text', text: r.b64 }],
      isError: r.status >= 400,
    });
  };
  const viaScrapling = async (): Promise<ToolResult> => {
    const r = await scraplingScreenshot({ url, fullPage, waitMs });
    return trace('web_screenshot', {
      content: [{ type: 'text', text: r.b64 }],
      isError: r.status >= 400,
    });
  };

  // Capturing what the routed visitor sees is the whole point: Italian and
  // challenge-host pages must be captured from Camoufox, everything else is
  // Scrapling's — with the other as fallback so one outage doesn't lose captures.
  const primaryIsCamoufox = isItalianSource(url) || prefersCamoufox(url);
  const primary = primaryIsCamoufox ? viaCamoufox : viaScrapling;
  const secondary = primaryIsCamoufox ? viaScrapling : viaCamoufox;
  const primaryName = primaryIsCamoufox ? 'camoufox' : 'scrapling';

  try {
    return await primary();
  } catch (err) {
    log(`web_screenshot: ${primaryName} failed, falling back:`, errMsg(err));
  }
  try {
    return await secondary();
  } catch (err) {
    return trace('web_screenshot', {
      content: [{ type: 'text', text: `web_screenshot error: ${errMsg(err)}` }],
      isError: true,
    });
  }
}

export async function web_pdf(params: Record<string, unknown>): Promise<ToolResult> {
  const url = params.url as string | undefined;
  if (!url) {
    return { content: [{ type: 'text', text: 'web_pdf error: missing required `url`' }], isError: true };
  }
  try {
    // Print-to-PDF is Chromium-only, so there is no Camoufox fallback here by
    // design: Firefox cannot print-to-PDF for us either way.
    const r = await scraplingPdf({ url, timeoutMs: 60_000 });
    return trace('web_pdf', {
      content: [{ type: 'text', text: r.b64 }],
      isError: r.status >= 400,
    });
  } catch (err) {
    return trace('web_pdf', {
      content: [{ type: 'text', text: `web_pdf error: ${errMsg(err)}` }],
      isError: true,
    });
  }
}

export async function web_execute_js(params: Record<string, unknown>): Promise<ToolResult> {
  const url = params.url as string | undefined;
  const scripts = params.scripts;
  if (!url || !Array.isArray(scripts) || scripts.length === 0) {
    return {
      content: [
        { type: 'text', text: 'web_execute_js error: `url` and a non-empty `scripts` array are required' },
      ],
      isError: true,
    };
  }
  try {
    const r = await scraplingEval({
      url,
      scripts: scripts as string[],
      timeoutMs: 60_000,
    });
    return trace('web_execute_js', {
      content: [
        {
          type: 'text',
          text: JSON.stringify({ status: r.status, url: r.url, mode: r.mode, results: r.results }),
        },
      ],
      isError: false,
    });
  } catch (err) {
    return trace('web_execute_js', {
      content: [{ type: 'text', text: `web_execute_js error: ${errMsg(err)}` }],
      isError: true,
    });
  }
}

/**
 * Crawl sequentially through the same pipeline as web_fetch: one fetch + one
 * markdown render per URL, in the order given.
 *
 * Sequential on purpose. Both sidecars serialize their work per browser mode
 * anyway (one single-slot executor), so a concurrent loop would only shuffle
 * the queue; and a caller watching progress wants the URLs in their own order.
 * The response shape — one payload, `results[]` with {url, status_code,
 * success, markdown} — is the contract existing callers parse.
 */
export async function web_crawl(params: Record<string, unknown>): Promise<ToolResult> {
  const urls = Array.isArray(params.urls) ? (params.urls as unknown[]).filter((u): u is string => typeof u === 'string') : [];
  if (urls.length === 0) {
    return {
      content: [{ type: 'text', text: 'web_crawl error: `urls` must be a non-empty array' }],
      isError: true,
    };
  }

  const results: Array<Record<string, unknown>> = [];
  const timeoutMs =
    typeof params.timeout_ms === 'number' && Number.isFinite(params.timeout_ms)
      ? params.timeout_ms
      : 60_000;
  const cssSelector = typeof params.css_selector === 'string' ? params.css_selector : undefined;
  for (const url of urls) {
    try {
      const page = await fetchPage(url, { timeoutMs });
      const markdown = page.html
        ? await scraplingRenderMarkdown({
            html: page.html,
            url: page.url || url,
            filter: 'fit',
            ...(cssSelector ? { cssSelector } : {}),
          })
        : '';
      results.push({
        url,
        status_code: page.status,
        success: page.status < 400 && !!markdown,
        mode: page.mode,
        markdown,
      });
    } catch (err) {
      // A failed URL must not sink the batch: report it in its own slot and
      // keep going, exactly as a per-URL failure would read in parallel mode.
      log(`web_crawl: ${url} failed:`, errMsg(err));
      results.push({ url, status_code: 0, success: false, error: errMsg(err) });
    }
  }

  return trace('web_crawl', {
    content: [{ type: 'text', text: JSON.stringify({ results }) }],
    isError: results.every((r) => r.success !== true),
  });
}

export async function web_snapshots(params: {
  url: string;
  from?: string;
  to?: string;
  limit?: number;
  match_type?: 'exact' | 'prefix' | 'host' | 'domain';
  filter?: string[];
}) {
  const snapshots = await getSnapshots({
    url: params.url,
    from: params.from,
    to: params.to,
    limit: params.limit,
    matchType: params.match_type,
    filter: params.filter,
  });
  traceJson('web_snapshots', snapshots);
  return snapshots;
}

export async function web_archive(params: {
  url: string;
  timestamp: string;
  original?: boolean;
}) {
  const { waybackUrl, content } = await getArchivedPage(params);
  const MAX_LENGTH = 50000;
  const truncated = content.length > MAX_LENGTH;
  const out = {
    waybackUrl,
    contentLength: content.length,
    content: truncated
      ? content.substring(0, MAX_LENGTH) + '\n\n[Content truncated]'
      : content,
  };
  traceJson('web_archive', out);
  return out;
}

// Process-local cost/usage counters. See stats.ts.
export async function web_usage_stats(_params: Record<string, unknown>) {
  return getStats();
}

// ── Camoufox-backed tools ────────────────────────────────────────────
// These expose what the Italian residential Firefox can do and the other
// backend cannot. They are separate tools rather than flags on the existing
// ones because the capability differs, not just the egress: a binary download
// and a warmed-session POST are not "web_fetch with an option".

/**
 * Download a URL's raw bytes through the residential exit, base64-encoded.
 *
 * Everything else here returns text. PDFs behind a residential/bot-gated origin
 * need the bytes as served — re-rendering them as markdown loses the document.
 */
export async function web_bytes(params: Record<string, unknown>): Promise<ToolResult> {
  const url = params.url as string | undefined;
  if (!url) {
    return { content: [{ type: 'text', text: 'web_bytes error: missing required `url`' }], isError: true };
  }
  try {
    const r = await camoufoxBytes({
      url,
      timeoutMs: typeof params.timeout_ms === 'number' ? params.timeout_ms : undefined,
    });
    return trace('web_bytes', {
      content: [{ type: 'text', text: JSON.stringify({ status: r.status, url, size_b64: r.b64.length, b64: r.b64 }) }],
      // A non-2xx is reported in `status`, not raised — a caller fetching a PDF
      // that 404s wants to know that, not to lose the response.
      isError: false,
    });
  } catch (err) {
    const msg = errMsg(err);
    log('web_bytes failed:', msg);
    return trace('web_bytes', { content: [{ type: 'text', text: `web_bytes error: ${msg}` }], isError: true });
  }
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
    return { ok: false, retryable: true, outcome: 'not_submitted', form_submissions: 0, status, error: reason };
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
  return { ok: false, retryable: false, outcome: 'unknown', form_submissions: null, status: status ?? 0, error: reason };
}

function formResult(tool: 'web_form_submit' | 'web_form_inspect', body: Record<string, unknown>, isError: boolean): ToolResult {
  return trace(tool, { content: [{ type: 'text', text: JSON.stringify(body) }], isError });
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
    });
    // An answered run is never auto-replayable, whatever its outcome: the
    // only sanctioned replay is the 503 below.
    return formResult('web_form_submit', { ...r, retryable: false }, false);
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

export async function web_eval(params: Record<string, unknown>): Promise<ToolResult> {
  const url = params.url as string | undefined;
  const js = params.js as string | undefined;
  if (!url || !js) {
    return {
      content: [{ type: 'text', text: 'web_eval error: `url` and `js` are both required' }],
      isError: true,
    };
  }
  try {
    const r = await camoufoxEval({
      url,
      js,
      waitUntil: params.wait_until as string | undefined,
      waitMs: typeof params.wait_ms === 'number' ? params.wait_ms : undefined,
      timeoutMs: typeof params.timeout_ms === 'number' ? params.timeout_ms : undefined,
      freshIp: params.fresh_ip === true,
    });
    return trace('web_eval', {
      content: [{ type: 'text', text: JSON.stringify({ status: r.status, url: r.url, result: r.result }) }],
      isError: false,
    });
  } catch (err) {
    const msg = errMsg(err);
    log('web_eval failed:', msg);
    return trace('web_eval', { content: [{ type: 'text', text: `web_eval error: ${msg}` }], isError: true });
  }
}

/**
 * Same-origin in-page fetch on a warmed page, for origins that gate POSTs on an
 * Akamai sensor cookie.
 *
 * Stateful, and the only tool here that is. The sidecar keeps one warmed page
 * per (base_url, warm_path), pins it to a sticky residential exit and feeds the
 * sensor on a keepalive so `_abck` stays validated (~0~); an unvalidated cookie
 * means the POST is refused at the edge. Treat the warmed session as a shared
 * resource: web_recycle, or anything that tears the browser down, costs whoever
 * is mid-crawl their maturation.
 */
export async function web_spa_fetch(params: Record<string, unknown>): Promise<ToolResult> {
  const baseUrl = params.base_url as string | undefined;
  const path = params.path as string | undefined;
  if (!baseUrl || !path) {
    return {
      content: [{ type: 'text', text: 'web_spa_fetch error: `base_url` and `path` are both required' }],
      isError: true,
    };
  }
  try {
    const r = await camoufoxSpaFetch({
      baseUrl,
      path,
      warmPath: params.warm_path as string | undefined,
      method: params.method as string | undefined,
      body: (params.body ?? undefined) as Record<string, unknown> | null | undefined,
      accept: params.accept as string | undefined,
      sensorWaitMs: typeof params.sensor_wait_ms === 'number' ? params.sensor_wait_ms : undefined,
      maturProbe: (params.mature_probe ?? undefined) as Record<string, unknown> | null | undefined,
      maturMaxTries: typeof params.mature_max_tries === 'number' ? params.mature_max_tries : undefined,
      timeoutMs: typeof params.timeout_ms === 'number' ? params.timeout_ms : undefined,
    });
    return trace('web_spa_fetch', {
      content: [{ type: 'text', text: JSON.stringify({ status: r.status, text: r.text }) }],
      // The upstream status is data: 403 means the sensor has not cleared and the
      // caller should re-mature or recycle, which is a decision, not an error.
      isError: false,
    });
  } catch (err) {
    const msg = errMsg(err);
    log('web_spa_fetch failed:', msg);
    return trace('web_spa_fetch', {
      content: [{ type: 'text', text: `web_spa_fetch error: ${msg}` }],
      isError: true,
    });
  }
}

/**
 * Drop the warmed session and the render browser, and mint a fresh exit IP.
 *
 * Expensive (a full relaunch, ~30-60s) and destructive to anyone mid-crawl. The
 * reason to reach for it is an exit IP the origin has rate-hardened, which does
 * not recover on its own. For a fresh IP on a single request, pass fresh_ip to
 * web_eval instead — a new context costs ~1s.
 */
export async function web_recycle(_params: Record<string, unknown>): Promise<ToolResult> {
  try {
    const r = await camoufoxRecycle();
    return { content: [{ type: 'text', text: JSON.stringify(r) }], isError: false };
  } catch (err) {
    const msg = errMsg(err);
    log('web_recycle failed:', msg);
    return { content: [{ type: 'text', text: `web_recycle error: ${msg}` }], isError: true };
  }
}

// ── Function map ─────────────────────────────────────────────────────

export const functionMap: Record<string, (params: any) => Promise<any>> = {
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
};
