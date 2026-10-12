import { Config } from './config.js';
import { log } from './log.js';
import type { SearchResult } from './types.js';
import { fetchYahoo } from './yahoo.js';

type SearXNGResult = { url: string; title: string; content: string; engine?: string; engines?: string[] };

type SearXNGResponse = {
  results: SearXNGResult[];
  /** [engine, reason] pairs, e.g. ["google", "CAPTCHA"]. */
  unresponsive_engines?: [string, string][];
};

export class SearXNGError extends Error {}

/**
 * One SearXNG request. Throws on failure — an unreachable SearXNG, an HTTP
 * error, or zero results while engines report themselves unresponsive —
 * so the failure is visible and counted, not an empty list that reads as
 * "nothing on the web matches". Zero results with healthy engines is a real
 * answer and comes back as [].
 */
async function fetchSearXNG(query: string, engines: string | undefined, page = 1): Promise<SearXNGResult[]> {
  const { url: baseUrl, engines: defaultEngines, categories } = Config.searxng;
  const params = new URLSearchParams({ q: query, format: 'json' });
  const chosen = engines || defaultEngines;
  if (chosen) params.set('engines', chosen);
  if (page > 1) params.set('pageno', String(page));
  if (categories) params.set('categories', categories);

  let response: Response;
  try {
    response = await fetch(`${baseUrl}/search?${params}`, {
      signal: AbortSignal.timeout(Config.requestTimeout * 1000),
      headers: { Accept: 'application/json' },
    });
  } catch (err) {
    throw new SearXNGError(`searxng unreachable: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) {
    throw new SearXNGError(`searxng HTTP ${response.status}: ${(await response.text().catch(() => '')).slice(0, 200)}`);
  }

  const body = (await response.json()) as SearXNGResponse;
  let valid = (body.results ?? []).filter((r) => r.title && r.url);
  // SearXNG treats an engine name it has not registered (disabled, or marked
  // inactive by the image defaults) as no choice at all and answers from its
  // default engines. That is how every engines=google search returned Bing
  // rows from 2026-10-02 to 2026-10-12. Keep only the engines asked for, and
  // say so when none of them answered.
  if (chosen) {
    const asked = new Set(chosen.split(',').map((e) => e.trim()).filter(Boolean));
    const enginesOf = (r: SearXNGResult) => r.engines ?? (r.engine ? [r.engine] : []);
    // A row with no engine metadata cannot be told apart, so it is kept.
    const fromAsked = (r: SearXNGResult) => enginesOf(r).length === 0 || enginesOf(r).some((e) => asked.has(e));
    const others = valid.filter((r) => !fromAsked(r));
    if (others.length > 0) {
      const answeredBy = [...new Set(others.flatMap(enginesOf))];
      valid = valid.filter(fromAsked);
      if (valid.length === 0) {
        throw new SearXNGError(
          `searxng: ${chosen} did not answer; results came from ${answeredBy.join(', ')} instead (engine not active?)`,
        );
      }
    }
  }
  const down = body.unresponsive_engines ?? [];
  log(`SearXNG: ${valid.length} results${down.length ? `, unresponsive: ${JSON.stringify(down)}` : ''}`);
  if (valid.length === 0 && down.length > 0) {
    throw new SearXNGError(
      `searxng: no results, engines unresponsive: ${down.map(([e, why]) => `${e} (${why})`).join(', ')}`,
    );
  }
  return valid;
}

/**
 * Several engines are asked one request each, in parallel, and merged
 * round-robin in the caller's order. Asked together in one SearXNG request,
 * Google contributed nothing whenever another engine was listed: "google"
 * alone returned the right LinkedIn profile in 1 s, "google,bing" returned
 * only Bing's 7 off-topic rows, and "google,brave" or "google,duckduckgo"
 * failed outright on Brave's 429 or DuckDuckGo's 15 s timeout (2026-10-12).
 * Apart, a blocked or slow engine costs only its own rows. It is an error
 * only when every engine fails.
 */
// Engines web-tools serves with its own code rather than through SearXNG.
// `yahoo` is a stealth-browser fetch on the residential exit (see yahoo.ts).
const OWN_ENGINES = new Set(['yahoo']);

function fetchEngine(query: string, engine: string, page: number): Promise<SearXNGResult[]> {
  return engine === 'yahoo' ? fetchYahoo(query, page) : fetchSearXNG(query, engine, page);
}

async function fetchEachEngine(query: string, engines: string[], page = 1): Promise<SearXNGResult[]> {
  const settled = await Promise.allSettled(engines.map((engine) => fetchEngine(query, engine, page)));
  const lists = settled.flatMap((s) => (s.status === 'fulfilled' ? [s.value] : []));
  if (lists.length === 0) {
    const reasons = settled.map((s, i) =>
      `${engines[i]}: ${s.status === 'rejected' && s.reason instanceof Error ? s.reason.message : String(s)}`,
    );
    throw new SearXNGError(`searxng: every engine failed (${reasons.join('; ')})`);
  }
  const merged: SearXNGResult[] = [];
  for (let i = 0; lists.some((l) => i < l.length); i++) {
    for (const list of lists) if (i < list.length) merged.push(list[i]!);
  }
  return merged;
}

export async function searchSearXNG(
  query: string,
  options?: { limit?: number; engines?: string; page?: number },
): Promise<SearchResult[]> {
  const limit = options?.limit ?? 10;
  const engines = (options?.engines ?? '').split(',').map((e) => e.trim()).filter(Boolean);
  const page = options?.page ?? 1;
  const raw =
    engines.length > 1 ? await fetchEachEngine(query, engines, page)
    : engines.length === 1 && OWN_ENGINES.has(engines[0]!) ? await fetchEngine(query, engines[0]!, page)
    : await fetchSearXNG(query, options?.engines, page);

  const seen = new Set<string>();
  const data: SearchResult[] = [];
  for (const r of raw) {
    if (seen.has(r.url)) continue;
    seen.add(r.url);
    data.push({ url: r.url, title: r.title || '', description: r.content || '' });
    if (data.length >= limit) break;
  }
  return data;
}
