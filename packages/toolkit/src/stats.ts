// Per-process running counters for cost monitoring. In-memory only — resets
// on container restart, and `started_at` lets callers detect that.
//
// Bytes are the size of the payload handed back (markdown / html / json /
// base64). The metered egress lives in the sidecars (Scrapling stealth, all of
// Camoufox), so their bandwidth is estimated from what this process returns.

import { TOOL_NAMES, type ToolName } from './types.js';

export type { ToolName };

type Counter = { calls: number; bytes: number; errors: number };

const startedAt = new Date().toISOString();

const byTool = Object.fromEntries(
  TOOL_NAMES.map((t) => [t, { calls: 0, bytes: 0, errors: 0 }]),
) as Record<ToolName, Counter>;

// Tools whose upstream MAY egress through a metered residential proxy, so
// their bytes are an upper bound (fast-mode Scrapling is free; this process
// cannot see which mode served a call, and over-counting is the safe side).
// web_archive / web_snapshots ride the residential exit through /raw because
// web.archive.org drops this project's datacenter IPs.
const PROXY_BACKED = new Set<ToolName>([
  'web_fetch',
  'web_html',
  'web_crawl',
  'web_screenshot',
  'web_pdf',
  'web_execute_js',
  'web_bytes',
  'web_eval',
  'web_form_submit',
  'web_form_inspect',
  'web_spa_fetch',
  'web_archive',
  'web_snapshots',
  'web_agent', // upper bound: only stealth=true runs egress on the residential proxy
]);

export function recordCall(tool: ToolName, payloadBytes: number, isError = false): void {
  const c = byTool[tool];
  c.calls++;
  c.bytes += payloadBytes;
  if (isError) c.errors++;
}

/** Test hook: zero every counter. */
export function resetStats(): void {
  for (const c of Object.values(byTool)) Object.assign(c, { calls: 0, bytes: 0, errors: 0 });
}

export function getStats() {
  const ratePerGB = Number(process.env.PROXY_USD_PER_GB ?? '10');
  // Upstream traffic (full HTML + scripts + images) is typically ~5–10× the
  // payload handed back. Tune via env to match the source's real ratio.
  const multiplier = Number(process.env.PROXY_BYTES_MULTIPLIER ?? '8');
  const counters = Object.entries(byTool) as [ToolName, Counter][];
  const proxied = counters.filter(([t]) => PROXY_BACKED.has(t)).map(([, c]) => c);
  const sum = (cs: Counter[], k: keyof Counter) => cs.reduce((a, c) => a + c[k], 0);

  const responseBytes = sum(proxied, 'bytes');
  const proxyBytes = Math.round(responseBytes * multiplier);
  const proxyGB = proxyBytes / 1024 ** 3;
  const all = counters.map(([, c]) => c);
  return {
    started_at: startedAt,
    rate_per_gb_usd: ratePerGB,
    bytes_multiplier: multiplier,
    total_calls: sum(all, 'calls'),
    total_errors: sum(all, 'errors'),
    proxy_calls: sum(proxied, 'calls'),
    response_bytes: responseBytes, // raw observed
    proxy_bytes: proxyBytes, // estimated upstream
    proxy_gb: proxyGB,
    estimated_usd: proxyGB * ratePerGB,
    by_tool: Object.fromEntries(counters.map(([t, c]) => [t, { ...c }])),
  };
}
