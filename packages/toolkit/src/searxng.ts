import { Config } from './config.js';
import { log } from './log.js';
import type { SearchResult } from './types.js';

type SearXNGResult = { url: string; title: string; content: string };

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
async function fetchSearXNG(query: string, engines: string | undefined): Promise<SearXNGResult[]> {
  const { url: baseUrl, engines: defaultEngines, categories } = Config.searxng;
  const params = new URLSearchParams({ q: query, format: 'json' });
  const chosen = engines || defaultEngines;
  if (chosen) params.set('engines', chosen);
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
  const valid = (body.results ?? []).filter((r) => r.title && r.url);
  const down = body.unresponsive_engines ?? [];
  log(`SearXNG: ${valid.length} results${down.length ? `, unresponsive: ${JSON.stringify(down)}` : ''}`);
  if (valid.length === 0 && down.length > 0) {
    throw new SearXNGError(
      `searxng: no results, engines unresponsive: ${down.map(([e, why]) => `${e} (${why})`).join(', ')}`,
    );
  }
  return valid;
}

export async function searchSearXNG(
  query: string,
  options?: { limit?: number; engines?: string },
): Promise<SearchResult[]> {
  const limit = options?.limit ?? 10;
  const raw = await fetchSearXNG(query, options?.engines);

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
