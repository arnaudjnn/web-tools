// Per-process running counters for cost monitoring. In-memory only —
// resets on container restart, and `startedAt` lets callers detect
// that. Persistence (Redis / file) is intentionally out of scope until
// we have a real need.
//
// `approxProxyBytes` tracks the size of the payload we return to the
// caller (markdown / html / json).
//
// NOTE: Crawl4AI-backed tools no longer egress through a residential proxy —
// Crawl4AI >= 0.9 refuses proxy_config from a request body, so these calls go
// out on the platform's own IP and cost no proxy bandwidth. The cost estimate
// below is therefore an upper bound kept for continuity; the metered proxy now
// sits in the Scrapling service. See config.ts.

export type ToolName =
  | 'web_search'
  | 'web_fetch'
  | 'web_html'
  | 'web_crawl'
  | 'web_screenshot'
  | 'web_pdf'
  | 'web_execute_js'
  | 'web_snapshots'
  | 'web_archive'
  | 'web_bytes'
  | 'web_eval'
  | 'web_form_submit'
  | 'web_spa_fetch';

const startedAt = new Date().toISOString();

const counts: Record<ToolName, number> = {
  web_search: 0,
  web_fetch: 0,
  web_html: 0,
  web_crawl: 0,
  web_screenshot: 0,
  web_pdf: 0,
  web_execute_js: 0,
  web_snapshots: 0,
  web_archive: 0,
  web_bytes: 0,
  web_eval: 0,
  web_form_submit: 0,
  web_spa_fetch: 0,
};

// Per-tool bytes of returned payload. Used as a proxy-bandwidth proxy.
const bytes: Record<ToolName, number> = {
  web_search: 0,
  web_fetch: 0,
  web_html: 0,
  web_crawl: 0,
  web_screenshot: 0,
  web_pdf: 0,
  web_execute_js: 0,
  web_snapshots: 0,
  web_archive: 0,
  web_bytes: 0,
  web_eval: 0,
  web_form_submit: 0,
  web_spa_fetch: 0,
};

const errors: Record<ToolName, number> = {
  web_search: 0,
  web_fetch: 0,
  web_html: 0,
  web_crawl: 0,
  web_screenshot: 0,
  web_pdf: 0,
  web_execute_js: 0,
  web_snapshots: 0,
  web_archive: 0,
  web_bytes: 0,
  web_eval: 0,
  web_form_submit: 0,
  web_spa_fetch: 0,
};

// Only Crawl4AI-backed tools accrue proxy bandwidth. SearXNG and
// Wayback are direct HTTP, not residential-proxied.
// Only the tools that egress through a metered residential proxy. The
// Crawl4AI-backed ones (web_crawl, web_screenshot, web_pdf, web_execute_js) used
// to be listed here and were inflating the estimate: Crawl4AI >= 0.9 refuses a
// proxy, so those go out on this host's own IP and cost nothing per byte.
//
// web_fetch and web_html stay, with a caveat: both fall back to Crawl4AI when a
// sidecar is unreachable, and a Crawl4AI-preferred host never touches the proxy
// at all, so their bytes are an upper bound rather than a measurement.
const PROXY_BACKED: ToolName[] = [
  'web_fetch',
  'web_html',
  'web_bytes',
  'web_eval',
  'web_form_submit',
  'web_spa_fetch',
];

export function recordCall(tool: ToolName, payloadBytes: number, isError = false): void {
  counts[tool]++;
  bytes[tool] += payloadBytes;
  if (isError) errors[tool]++;
}

export function getStats() {
  const ratePerGB = Number(process.env.PROXY_USD_PER_GB ?? '10');
  // We measure the size of the response payload we hand back (markdown
  // for web_fetch, html/json for web_crawl, etc.). The upstream proxy
  // traffic is the full rendered HTML + scripts + images that Crawl4AI
  // pulled to produce that payload — typically ~5–10× larger. Tune via
  // env to match the source's real ratio.
  const multiplier = Number(process.env.PROXY_BYTES_MULTIPLIER ?? '8');
  const responseBytes = PROXY_BACKED.reduce((a, t) => a + bytes[t], 0);
  const proxyCalls = PROXY_BACKED.reduce((a, t) => a + counts[t], 0);
  const proxyBytes = Math.round(responseBytes * multiplier);
  const proxyGB = proxyBytes / 1024 ** 3;
  const estUsd = proxyGB * ratePerGB;
  const totalCalls = (Object.values(counts) as number[]).reduce((a, n) => a + n, 0);
  const totalErrors = (Object.values(errors) as number[]).reduce((a, n) => a + n, 0);
  return {
    started_at: startedAt,
    rate_per_gb_usd: ratePerGB,
    bytes_multiplier: multiplier,
    total_calls: totalCalls,
    total_errors: totalErrors,
    proxy_calls: proxyCalls,
    response_bytes: responseBytes, // raw observed
    proxy_bytes: proxyBytes,       // estimated upstream
    proxy_gb: proxyGB,
    estimated_usd: estUsd,
    by_tool: Object.fromEntries(
      (Object.keys(counts) as ToolName[]).map((t) => [
        t,
        { calls: counts[t], bytes: bytes[t], errors: errors[t] },
      ]),
    ),
  };
}
