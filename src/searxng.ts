import { Config } from './config.js';

export type SearchResult = {
  url: string;
  title: string;
  description: string;
};

type SearXNGResult = {
  url: string;
  title: string;
  content: string;
};

type SearXNGResponse = {
  results: SearXNGResult[];
};

export async function searchSearXNG(
  query: string,
  options?: { limit?: number; timeout?: number; engines?: string },
): Promise<{ data: SearchResult[] }> {
  const { url: baseUrl, engines: defaultEngines, categories } = Config.searxng;
  const limit = options?.limit ?? 10;
  const timeout = options?.timeout ?? 30_000;

  const params = new URLSearchParams({
    q: query,
    format: 'json',
  });

  // Per-request engines override the default
  const engines = options?.engines || defaultEngines;
  if (engines) {
    params.set('engines', engines);
  }
  if (categories) {
    params.set('categories', categories);
  }

  const response = await fetch(`${baseUrl}/search?${params.toString()}`, {
    signal: AbortSignal.timeout(timeout),
    headers: { Accept: 'application/json' },
  });

  if (!response.ok) {
    throw new Error(`SearXNG search failed: ${response.status} ${response.statusText}`);
  }

  const body = (await response.json()) as SearXNGResponse;

  // Deduplicate by URL and limit results
  const seen = new Set<string>();
  const data: SearchResult[] = [];

  for (const r of body.results) {
    if (!r.url || seen.has(r.url)) continue;
    seen.add(r.url);
    data.push({
      url: r.url,
      title: r.title || '',
      description: r.content || '',
    });
    if (data.length >= limit) break;
  }

  return { data };
}
