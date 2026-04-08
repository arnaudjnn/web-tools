import { Config } from './config.js';
import type { SearchResult } from './types.js';

type SearXNGResult = {
  url: string;
  title: string;
  content: string;
};

type SearXNGResponse = {
  results: SearXNGResult[];
};

const log = (...args: unknown[]) => {
  process.stderr.write(
    args.map((a) => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ') + '\n',
  );
};

/** Single SearXNG request. Returns parsed results or null on failure. */
async function fetchSearXNG(
  query: string,
  options: { engines?: string; timeout: number },
  attempt: number,
): Promise<{ results: SearXNGResult[]; hasContent: boolean } | null> {
  try {
    const { url: baseUrl, engines: defaultEngines, categories } = Config.searxng;
    const params = new URLSearchParams({ q: query, format: 'json' });

    const engines = options.engines || defaultEngines;
    if (engines) params.set('engines', engines);
    if (categories) params.set('categories', categories);

    const response = await fetch(`${baseUrl}/search?${params.toString()}`, {
      signal: AbortSignal.timeout(options.timeout * 1000),
      headers: { Accept: 'application/json' },
    });

    if (!response.ok) {
      log(`SearXNG attempt ${attempt}: HTTP ${response.status}`);
      return null;
    }

    const body = (await response.json()) as SearXNGResponse;
    const valid = body.results.filter((r) => r.title && r.url);
    const withContent = valid.filter((r) => r.content?.trim());

    if (valid.length === 0) {
      log(`SearXNG attempt ${attempt}: 0 valid results`);
      return null;
    }

    log(
      `SearXNG attempt ${attempt}: ${valid.length} results (${withContent.length} with content)`,
    );

    return { results: valid, hasContent: withContent.length > 0 };
  } catch (err) {
    log(`SearXNG attempt ${attempt} failed:`, err instanceof Error ? err.message : String(err));
    return null;
  }
}

/** Fire parallel requests to SearXNG, return the first valid response with content. */
export async function searchSearXNG(
  query: string,
  options?: { limit?: number; engines?: string },
): Promise<{ data: SearchResult[] }> {
  const limit = options?.limit ?? 10;
  const timeout = Config.requestTimeout;
  const count = Config.parallelRequests;

  const tasks = Array.from({ length: count }, (_, i) =>
    fetchSearXNG(query, { engines: options?.engines, timeout }, i + 1),
  );

  // Return the first response that has results with content.
  // If none have content, return the first with any results.
  let bestNoContent: SearXNGResult[] | null = null;
  let rawResults: SearXNGResult[] = [];

  for (const promise of raceAll(tasks)) {
    const result = await promise;
    if (result === null) continue;

    if (result.hasContent) {
      rawResults = result.results;
      break;
    }
    if (bestNoContent === null) {
      bestNoContent = result.results;
    }
  }

  if (rawResults.length === 0) {
    rawResults = bestNoContent ?? [];
  }

  // Deduplicate by URL and limit results
  const seen = new Set<string>();
  const data: SearchResult[] = [];

  for (const r of rawResults) {
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

/** Yields promises in the order they resolve (like Promise.race but iterative). */
function raceAll<T>(promises: Promise<T>[]): Promise<T>[] {
  const results: { resolve: (value: T) => void; promise: Promise<T> }[] = [];
  const pending = new Set<Promise<T>>(promises);

  for (let i = 0; i < promises.length; i++) {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>((r) => { resolve = r; });
    results.push({ resolve, promise });
  }

  let idx = 0;
  for (const p of promises) {
    p.then(
      (value) => {
        if (pending.delete(p)) {
          results[idx++]!.resolve(value);
        }
      },
      () => {
        if (pending.delete(p)) {
          results[idx++]!.resolve(null as T);
        }
      },
    );
  }

  return results.map((r) => r.promise);
}
