import {
  callCrawlTool,
  callExecuteJsTool,
  callPdfTool,
  callScreenshotTool,
  renderMarkdown,
} from './crawl4ai.js';
import {
  camoufoxBytes,
  camoufoxEval,
  camoufoxRecycle,
  camoufoxRender,
  camoufoxScreenshot,
  camoufoxSpaFetch,
  camoufoxFormSubmit,
} from './camoufox.js';
import { isItalianSource, prefersCrawl4ai } from './routing.js';
import { scraplingFetch } from './scrapling.js';
import { searchSearXNG } from './searxng.js';
import { getStats, recordCall, type ToolName } from './stats.js';
import { getArchivedPage, getSnapshots } from './wayback.js';
import type { ToolResult } from './types.js';

// Upstream failure modes worth counting as errors in /stats even when
// Crawl4AI reports them as a 200 with the error JSON in the body: explicit
// anti-bot signals (429, CF challenge) and internal errors that trace back to
// a wedged browser context. Note Crawl4AI >= 0.9 also surfaces a plain
// anti-bot 403 as an opaque HTTP 500 + correlation id, so a 500 from it is not
// necessarily a server fault.
const BLOCK_RE =
  /HTTP 429|Too Many Requests|Cloudflare JS challenge|anti-bot protection|Just a moment\.\.\.|Unexpected error in _crawl_web|BrowserContext\.new_page|Navigation timeout|Connection closed while reading from the driver/i;

// Count a tool invocation: bytes = size of the text payload we hand back to
// the caller. There is deliberately no browser-rotation hook here any more.
// The old rotation module killed Crawl4AI's hot browser on N consecutive
// blocks so the next call would re-dial the residential proxy and land on a
// fresh exit IP. Crawl4AI >= 0.9 rejects proxy_config outright, so there is no
// proxy connection to re-dial: killing browsers could not change our egress IP,
// it only churned the pool on every LinkedIn 999.
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

// ── Crawl4AI proxy wrapper ───────────────────────────────────────────

async function proxyCrawl4AI(
  toolName: string,
  fn: () => Promise<unknown>,
): Promise<ToolResult> {
  try {
    const resolved = (await fn()) as ToolResult;

    if (resolved?.isError) {
      const text =
        resolved.content?.[0]?.text ||
        JSON.stringify(resolved.content) ||
        '(no details returned)';
      log(`Crawl4AI ${toolName} error response:`, text);
      return {
        content: [{ type: 'text', text: `Crawl4AI ${toolName} error: ${text}` }],
        isError: true,
      };
    }

    if (
      !resolved?.content ||
      resolved.content.length === 0 ||
      resolved.content.every((c) => !c.text)
    ) {
      log(`Crawl4AI ${toolName} returned empty content`);
      return {
        content: [
          {
            type: 'text',
            text: `Crawl4AI ${toolName} returned empty content. The page may have no extractable text or the crawl may have timed out.`,
          },
        ],
        isError: true,
      };
    }

    // Crawl4AI's MCP layer reports upstream HTTP failures as a SUCCESSFUL tool
    // call whose text is an error envelope — `{"error": 500, "detail": ...}` —
    // with isError absent. Left alone, that envelope flows all the way out as if
    // it were page content: a caller asking for a Trustpilot page got
    // `{"error": 500, ...}` back with isError:false. Detect the envelope and
    // call it what it is.
    const first = resolved.content?.[0]?.text ?? '';
    if (first.startsWith('{')) {
      try {
        const parsed = JSON.parse(first) as { error?: unknown; detail?: unknown };
        if (parsed && typeof parsed === 'object' && 'error' in parsed && !('results' in parsed)) {
          log(`Crawl4AI ${toolName} error envelope:`, first.slice(0, 300));
          return {
            content: [{ type: 'text', text: `Crawl4AI ${toolName} error: ${first}` }],
            isError: true,
          };
        }
      } catch {
        // Not JSON after all — fall through and treat it as content.
      }
    }

    return resolved;
  } catch (error) {
    const msg = error instanceof Error ? error.message : String(error);
    log(`Crawl4AI ${toolName} threw:`, msg);
    return {
      content: [{ type: 'text', text: `Crawl4AI ${toolName} error: ${msg}` }],
      isError: true,
    };
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

/**
 * Fetch a page through the backend the host calls for.
 *
 * Both web_fetch and web_html need this and used to each carry their own copy of
 * the Camoufox-to-page mapping, which is how web_html ended up without the
 * Crawl4AI host preference that web_fetch had: the same URL was fast through one
 * tool and slow through the other. One function, one routing decision.
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

  if (isItalianSource(url)) {
    const r = await camoufoxRender({ url, timeoutMs, ...opts });
    return {
      status: r.status,
      url: r.url,
      html: r.html,
      size: r.html.length,
      mode: 'camoufox',
      escalated: false,
    };
  }

  // No mode passed: the sidecar routes by host and escalates to a challenge
  // solve only on evidence. It takes networkIdle rather than the full set of
  // page knobs, so the rest are honoured where supported and dropped here.
  return scraplingFetch({ url, timeoutMs, networkIdle: opts.waitUntil === 'networkidle' });
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

/** Pull markdown for the requested filter out of a Crawl4AI `crawl` result. */
function markdownFromCrawlResult(
  resp: ToolResult,
  filter: string,
): { md: string; success: boolean } | null {
  const text = resp?.content?.[0]?.text;
  if (!text) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }
  const r = (parsed as { results?: Array<Record<string, unknown>> })?.results?.[0];
  if (!r) return null;

  const m = r.markdown as string | { raw_markdown?: string; fit_markdown?: string } | undefined;
  let md = '';
  if (typeof m === 'string') {
    md = m;
  } else if (m && typeof m === 'object') {
    md =
      (filter === 'raw' ? m.raw_markdown : m.fit_markdown) ||
      m.raw_markdown ||
      m.fit_markdown ||
      '';
  }
  if (!md) return null;
  return { md, success: r.success !== false };
}

/** Fetch a URL through Crawl4AI directly. The fallback when Scrapling is down. */
async function crawl4aiFetch(url: string, filter: string, delay: number): Promise<ToolResult> {
  return proxyCrawl4AI('crawl', async () => {
    const resp = (await callCrawlTool({
      urls: [url],
      // Only fields Crawl4AI >= 0.9 accepts from a request body. No
      // proxy_config / session_id (forbidden -> hard 400) and no
      // user_agent_mode:'random' (see stripPoolHostileFields).
      browser_config: {
        type: 'BrowserConfig',
        params: { headless: true, enable_stealth: true },
      },
      crawler_config: {
        type: 'CrawlerRunConfig',
        params: {
          wait_until: 'load',
          // Clamped to 60s server-side anyway, so ask honestly.
          page_timeout: 60000,
          delay_before_return_html: delay,
        },
      },
    })) as ToolResult;

    const extracted = markdownFromCrawlResult(resp, filter);
    if (!extracted) return resp;
    return {
      content: [{ type: 'text', text: extracted.md }],
      isError: !extracted.success,
    };
  });
}

export async function web_fetch(params: Record<string, unknown>): Promise<ToolResult> {
  const url = params.url as string | undefined;
  if (!url) {
    return {
      content: [{ type: 'text', text: 'web_fetch error: missing required `url`' }],
      isError: true,
    };
  }
  const filter = ((params.f as string | undefined) ?? 'fit').toLowerCase();
  const delay =
    typeof params.delay === 'number' && Number.isFinite(params.delay) ? params.delay : 2;

  // Which backend fetches this is decided from the host (see routing.ts), never
  // asked of the caller. Italian sources need an Italian residential visitor —
  // a US exit is the wrong country, not a milder version of the right one.
  // Some hosts are served best by the plain datacenter browser — going through a
  // stealth path would only spend a timeout before falling back here anyway.
  if (prefersCrawl4ai(url)) {
    return trace('web_fetch', await crawl4aiFetch(url, filter, delay));
  }

  let result: ToolResult;
  try {
    const page = await fetchPage(url, { timeoutMs: 60_000 });
    // Pass the URL we actually landed on (after redirects) so relative links
    // resolve against the right origin.
    const md = page.html
      ? await renderMarkdown(page.html, filter, params.q as string | undefined, page.url || url)
      : null;

    if (md) {
      // A block/challenge page converts to markdown perfectly well, so the
      // HTTP status is the only honest signal here — not whether we got text.
      // Keep the body either way: callers can often still use it, and it makes
      // "which wall did we hit" diagnosable.
      const blocked = page.status >= 400;
      const provenance = `scrapling mode=${page.mode}${page.escalated ? ', escalated' : ''}`;
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
    } else if (page.status === 200) {
      // Fetched fine but the markdown render produced nothing. Retry the whole
      // thing through Crawl4AI rather than reporting an empty page: it fetches
      // and renders in one step, so it cannot hit this particular seam.
      log(`web_fetch: no markdown from ${page.size} bytes (mode=${page.mode}); retrying via Crawl4AI`);
      result = await crawl4aiFetch(url, filter, delay);
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
  } catch (err) {
    // Scrapling unreachable or erroring: fall back to Crawl4AI so a sidecar
    // outage degrades quality rather than failing the tool outright.
    log(
      `web_fetch: stealth fetcher unavailable, falling back to Crawl4AI:`,
      err instanceof Error ? err.message : String(err),
    );
    result = await crawl4aiFetch(url, filter, delay);
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

  // Same host preference as web_fetch. Without this the two tools disagree about
  // which backend serves a host, so the same URL is fast through one and pays a
  // stealth timeout through the other.
  if (prefersCrawl4ai(url)) {
    return trace('web_html', await crawl4aiHtml(url));
  }

  try {
    const page = await fetchPage(url, pageOptsFrom(params, timeoutMs));
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
  } catch (err) {
    // Same fallback as web_fetch: a stack without the Scrapling service (i.e.
    // deployed from the template before it existed) still gets HTML, just
    // without residential egress or challenge solving.
    log(
      'web_html: stealth fetcher unavailable, falling back to Crawl4AI:',
      err instanceof Error ? err.message : String(err),
    );
    return trace('web_html', await crawl4aiHtml(url));
  }
}

/** Raw HTML via Crawl4AI. The fallback when Scrapling is unavailable. */
async function crawl4aiHtml(url: string): Promise<ToolResult> {
  return proxyCrawl4AI('crawl', async () => {
    const resp = (await callCrawlTool({
      urls: [url],
      browser_config: {
        type: 'BrowserConfig',
        params: { headless: true, enable_stealth: true },
      },
      crawler_config: {
        type: 'CrawlerRunConfig',
        params: { wait_until: 'load', page_timeout: 60000, delay_before_return_html: 2 },
      },
    })) as ToolResult;

    const text = resp?.content?.[0]?.text;
    if (!text) return resp;
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      return resp;
    }
    const r = (parsed as { results?: Array<Record<string, unknown>> })?.results?.[0];
    const html = typeof r?.html === 'string' ? r.html : '';
    if (!html) return resp;

    return {
      content: [
        {
          type: 'text',
          text: JSON.stringify({
            status: typeof r?.status_code === 'number' ? r.status_code : 200,
            url,
            // Report the engine honestly so a degraded stack is visible in the
            // response rather than silently looking like a stealth fetch.
            mode: 'crawl4ai',
            escalated: false,
            size: html.length,
            html,
          }),
        },
      ],
      isError: false,
    };
  });
}

export async function web_screenshot(params: Record<string, unknown>): Promise<ToolResult> {
  const url = params.url as string | undefined;

  // Same host routing as web_fetch: capturing what an Italian residential
  // visitor sees is the whole point for these sources, and Crawl4AI would
  // screenshot a bot wall from this host's datacenter IP instead.
  if (url && isItalianSource(url)) {
    try {
      const r = await camoufoxScreenshot({
        url,
        fullPage: params.full_page !== false,
        waitMs:
          typeof params.screenshot_wait_for === 'number'
            ? params.screenshot_wait_for * 1000
            : undefined,
      });
      return trace('web_screenshot', {
        content: [{ type: 'text', text: r.b64 }],
        isError: r.status >= 400,
      });
    } catch (err) {
      log(
        'web_screenshot: camoufox unavailable, falling back to Crawl4AI:',
        err instanceof Error ? err.message : String(err),
      );
    }
  }

  return proxyCrawl4AI('screenshot', () => callScreenshotTool(params)).then((r) =>
    trace('web_screenshot', r),
  );
}

export async function web_pdf(params: Record<string, unknown>): Promise<ToolResult> {
  return proxyCrawl4AI('pdf', () => callPdfTool(params)).then((r) => trace('web_pdf', r));
}

export async function web_execute_js(params: Record<string, unknown>): Promise<ToolResult> {
  return proxyCrawl4AI('execute_js', () => callExecuteJsTool(params)).then((r) =>
    trace('web_execute_js', r),
  );
}

// Crawl4AI pools browsers by a SHA1 of the ENTIRE BrowserConfig
// (crawler_pool._sig), so any field that varies per request mints a brand new
// ~180MB Chromium that is never reused. `user_agent_mode: 'random'` does
// exactly that: measured 10 live browsers / 1890MB / reuse_rate_percent: 0,
// then `RuntimeError: can't start new thread` — after which the service 500s
// on EVERY request, not just the crawl that caused it. A caller asking for it
// would take down web_screenshot/web_pdf/web_crawl along with itself, so drop
// it here rather than trusting callers. A fixed `user_agent` string is fine:
// it is one stable signature, and was measured at 100% pool reuse.
function stripPoolHostileFields(bcParams: Record<string, unknown>): Record<string, unknown> {
  if (bcParams.user_agent_mode !== 'random') return bcParams;
  const { user_agent_mode: _dropped, ...rest } = bcParams;
  log(
    "web_crawl: dropped browser_config.user_agent_mode='random' — it defeats Crawl4AI's " +
      'browser pool (new Chromium per call) and exhausts the container.',
  );
  return rest;
}

export async function web_crawl(params: Record<string, unknown>): Promise<ToolResult> {
  // Default sensible browser config when the caller didn't set their own.
  // Keeps web_crawl symmetric with web_fetch. No proxy_config: Crawl4AI >= 0.9
  // rejects it outright (see config.ts).
  const bc = (params.browser_config as { params?: Record<string, unknown> } | undefined) ?? {};
  const bcParams = stripPoolHostileFields(bc.params ?? {});
  params = {
    ...params,
    browser_config: {
      type: 'BrowserConfig',
      params: {
        headless: true,
        enable_stealth: true,
        ...bcParams,
      },
    },
  };
  return proxyCrawl4AI('crawl', () => callCrawlTool(params)).then((r) =>
    trace('web_crawl', r),
  );
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
// These expose what the Italian residential Firefox can do and the other two
// backends cannot. They are separate tools rather than flags on the existing
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
    const msg = err instanceof Error ? err.message : String(err);
    log('web_bytes failed:', msg);
    return trace('web_bytes', { content: [{ type: 'text', text: `web_bytes error: ${msg}` }], isError: true });
  }
}

/**
 * Evaluate JS in a residential page and return its JSON result.
 *
 * web_execute_js already runs scripts, but through Crawl4AI on this host's
 * datacenter IP — useless for a site that bot-gates that IP. This is the same
 * idea from an Italian residential Firefox, for driving/inspecting JS SPAs
 * (open a facet dropdown, read the codes behind it).
 */
export async function web_form_submit(params: Record<string, unknown>): Promise<ToolResult> {
  const url = params.url as string | undefined;
  const submit = params.submit as string | undefined;
  const fields = params.fields as Array<Record<string, unknown>> | undefined;
  if (!url || !submit || !Array.isArray(fields)) {
    return {
      content: [{ type: 'text', text: 'web_form_submit error: `url`, `fields` and `submit` are required' }],
      isError: true,
    };
  }
  try {
    const r = await camoufoxFormSubmit({
      url,
      submit,
      fields: fields as never,
      dismiss: params.dismiss as string[] | undefined,
      successUrl: params.success_url as string | undefined,
      waitUntil: params.wait_until as string | undefined,
      waitMs: typeof params.wait_ms === 'number' ? params.wait_ms : undefined,
      settleMs: typeof params.settle_ms === 'number' ? params.settle_ms : undefined,
      timeoutMs: typeof params.timeout_ms === 'number' ? params.timeout_ms : undefined,
      freshIp: params.fresh_ip !== false,
    });
    return trace('web_form_submit', {
      content: [{ type: 'text', text: JSON.stringify({ status: r.status, url: r.url, ok: r.ok, html: r.html }) }],
      isError: false,
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    log('web_form_submit failed:', msg);
    return trace('web_form_submit', {
      content: [{ type: 'text', text: `web_form_submit error: ${msg}` }],
      isError: true,
    });
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
    const msg = err instanceof Error ? err.message : String(err);
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
    const msg = err instanceof Error ? err.message : String(err);
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
    const msg = err instanceof Error ? err.message : String(err);
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
  web_spa_fetch,
  web_recycle,
};
