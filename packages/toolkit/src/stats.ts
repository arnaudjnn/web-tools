// Per-process running counters for cost monitoring. In-memory only —
// resets on container restart, and `startedAt` lets callers detect
// that. Persistence (Redis / file) is intentionally out of scope until
// we have a real need.
//
// `approxProxyBytes` tracks the size of the payload we return to the
// caller (markdown / html / json).
//
// The metered egress lives in the sidecars (Scrapling's stealth mode, all of
// Camoufox); this process only estimates their bandwidth from what it hands
// back. See PROXY_BACKED below.

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
  | 'web_spa_fetch'
  | 'web_agent';

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
  web_agent: 0,
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
  web_agent: 0,
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
  web_agent: 0,
};

// Tools whose upstream fetch MAY egress through a metered residential proxy
// (Scrapling stealth mode, Camoufox always), so their payload bytes are an
// upper bound: fast-mode Scrapling egresses on the platform's own IP and costs
// nothing per byte, and web_search is direct — SearXNG does its own egress.
// web_archive / web_snapshots moved INTO this list on 2026-09-27: web.archive.org
// silently drops this project's datacenter IPs, so both now ride the sidecar's
// residential exit through /raw (bodies are KB–MB; the cost is real but small).
// Counted as upper bound rather than measured — this process cannot see
// which sidecar mode actually served a call, and over-counting a cost estimate
// is the safe direction.
const PROXY_BACKED: ToolName[] = [
  'web_fetch',
  'web_html',
  'web_crawl',
  'web_screenshot',
  'web_pdf',
  'web_execute_js',
  'web_bytes',
  'web_eval',
  'web_form_submit',
  'web_spa_fetch',
  'web_archive',
  'web_snapshots',
  'web_agent', // upper bound: only stealth=true runs egress on the residential proxy
];

export function recordCall(tool: ToolName, payloadBytes: number, isError = false): void {
  counts[tool]++;
  bytes[tool] += payloadBytes;
  if (isError) errors[tool]++;
}

export function getStats() {
  const ratePerGB = Number(process.env.PROXY_USD_PER_GB ?? '10');
  // We measure the size of the response payload we hand back (markdown
  // for web_fetch, html/json for web_html, etc.). The upstream proxy
  // traffic is the full rendered HTML + scripts + images the sidecar
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
